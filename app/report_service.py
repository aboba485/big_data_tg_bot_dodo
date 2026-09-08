from __future__ import annotations

import asyncio
import logging
import time
import uuid
from typing import Any

from app.config import Settings
from app.dodo.executor import DodoExecutor
from app.dodo.units import UnitResolver
from app.errors import ApplicationError
from app.planner.schemas import OutputFormat, PlanMode, PlanStatus, ReportPlan
from app.planner.service import PlannerService
from app.planner.validator import ReportPlanValidator
from app.reports.exporters import export_csv, export_xlsx
from app.reports.matrix import period_label_from_dates
from app.reports.service import ReportAggregator, ReportResult
from app.retrieval.service import RetrievalService
from app.storage.generated_files import GeneratedFileRepository
from app.storage.report_runs import ReportRunRepository

logger = logging.getLogger(__name__)


class ReportOrchestrator:
    def __init__(
        self,
        settings: Settings,
        retrieval: RetrievalService,
        planner: PlannerService,
        validator: ReportPlanValidator,
        unit_resolver: UnitResolver,
        executor: DodoExecutor,
        aggregator: ReportAggregator,
        runs: ReportRunRepository,
        files: GeneratedFileRepository,
    ) -> None:
        self.settings = settings
        self.retrieval = retrieval
        self.planner = planner
        self.validator = validator
        self.unit_resolver = unit_resolver
        self.executor = executor
        self.aggregator = aggregator
        self.runs = runs
        self.files = files

    async def create_report(
        self, query: str, output_format: OutputFormat | None = None
    ) -> dict[str, Any]:
        request_id = str(uuid.uuid4())
        started = time.perf_counter()
        self.runs.create(request_id, query)
        logger.info("report_started request_id=%s query_length=%s", request_id, len(query))
        try:
            candidates = await self.retrieval.search_endpoints(query)
            planner_result = await self.planner.create_plan(query, candidates)
            plan = planner_result.plan
            self.validator.apply_query_intent(plan, query)
            if output_format is not None:
                plan.output_format = output_format
            elif plan.output_format == OutputFormat.SHEETS:
                # This transport cannot upload to a user's Drive; fall back to an inline table.
                plan.output_format = OutputFormat.TABLE
            if plan.status == PlanStatus.NEEDS_CLARIFICATION:
                response = {
                    "request_id": request_id,
                    "status": plan.status,
                    "question": plan.clarification_question,
                    "options": [],
                }
                self._finish(request_id, started, response["status"], planner_result, candidates)
                return response
            if plan.status == PlanStatus.UNSUPPORTED:
                response = {
                    "request_id": request_id,
                    "status": plan.status,
                    "reason": plan.unsupported_reason,
                }
                self._finish(request_id, started, response["status"], planner_result, candidates)
                return response
            unit_ids: list[str] = []
            if self.needs_units(plan):
                resolution = self.unit_resolver.resolve(plan.unit_references)
                if resolution.status != "ready":
                    response = {
                        "request_id": request_id,
                        "status": resolution.status,
                        "question": resolution.question,
                        "options": resolution.options,
                    }
                    self._finish(
                        request_id, started, response["status"], planner_result, candidates
                    )
                    return response
                unit_ids = resolution.unit_ids
            self.validator.validate(plan, candidates, unit_ids)
            if plan.status == PlanStatus.NEEDS_CLARIFICATION:
                response = {
                    "request_id": request_id,
                    "status": plan.status,
                    "question": plan.clarification_question,
                    "options": [],
                }
                self._finish(request_id, started, response["status"], planner_result, candidates)
                return response
            if plan.status == PlanStatus.UNSUPPORTED:
                response = {
                    "request_id": request_id,
                    "status": plan.status,
                    "reason": plan.unsupported_reason,
                }
                self._finish(request_id, started, response["status"], planner_result, candidates)
                return response
            return await self._execute_ready(
                request_id,
                started,
                query,
                plan,
                unit_ids,
                planner_result,
                candidates,
            )
        except asyncio.CancelledError:
            self.runs.finish(
                request_id,
                status="error",
                duration_ms=int((time.perf_counter() - started) * 1000),
                error_code="cancelled",
            )
            raise
        except ApplicationError as exc:
            self.runs.finish(
                request_id,
                status="error",
                duration_ms=int((time.perf_counter() - started) * 1000),
                error_code=exc.code,
            )
            raise
        except Exception:
            self.runs.finish(
                request_id,
                status="error",
                duration_ms=int((time.perf_counter() - started) * 1000),
                error_code="unexpected_error",
            )
            raise

    async def create_prepared_report(
        self,
        query: str,
        plan: ReportPlan,
        unit_ids: list[str],
        planner_result: Any,
        candidates: list[Any],
    ) -> dict[str, Any]:
        request_id = str(uuid.uuid4())
        started = time.perf_counter()
        self.runs.create(request_id, query)
        logger.info("report_started request_id=%s query_length=%s", request_id, len(query))
        try:
            return await self._execute_ready(
                request_id,
                started,
                query,
                plan,
                unit_ids,
                planner_result,
                candidates,
            )
        except asyncio.CancelledError:
            self.runs.finish(
                request_id,
                status="error",
                duration_ms=int((time.perf_counter() - started) * 1000),
                error_code="cancelled",
            )
            raise
        except ApplicationError as exc:
            self.runs.finish(
                request_id,
                status="error",
                duration_ms=int((time.perf_counter() - started) * 1000),
                error_code=exc.code,
            )
            raise
        except Exception:
            self.runs.finish(
                request_id,
                status="error",
                duration_ms=int((time.perf_counter() - started) * 1000),
                error_code="unexpected_error",
            )
            raise

    async def _execute_ready(
        self,
        request_id: str,
        started: float,
        query: str,
        plan: ReportPlan,
        unit_ids: list[str],
        planner_result: Any,
        candidates: list[Any],
    ) -> dict[str, Any]:
        with self.executor.client.request_scope() as scoped_requests:
            report = await self._run(plan, unit_ids)
        dodo_requests = scoped_requests[0]
        download = self._export(
            request_id,
            query,
            plan,
            unit_ids,
            report.columns,
            report.rows,
            report.totals,
        )
        duration_ms = int((time.perf_counter() - started) * 1000)
        response = {
            "request_id": request_id,
            "status": "ready",
            "query": query,
            "plan": {
                "mode": plan.mode,
                "metrics": plan.metric_ids,
                "operations": plan.operation_ids,
                "date_from": plan.date_from,
                "date_to": plan.date_to,
                "granularity": plan.granularity,
                "group_by": plan.group_by,
                "filters": [item.model_dump() for item in plan.filters],
                "raw_collection": plan.raw_collection,
                "operation_arguments": [item.model_dump() for item in plan.operation_arguments],
            },
            "execution": {
                "dodo_requests_count": dodo_requests,
                "endpoint_ids": plan.operation_ids,
                "duration_ms": duration_ms,
                "warnings": [],
                "openai_input_tokens": planner_result.usage.input_tokens,
                "openai_output_tokens": planner_result.usage.output_tokens,
            },
            "columns": report.columns,
            "rows": report.rows,
            "totals": report.totals,
            "summary": report.summary,
            "download": download,
        }
        self.runs.finish(
            request_id,
            status="ready",
            metric_ids=plan.metric_ids,
            operation_ids=plan.operation_ids,
            candidate_count=len(candidates),
            dodo_requests_count=dodo_requests,
            openai_input_tokens=planner_result.usage.input_tokens,
            openai_output_tokens=planner_result.usage.output_tokens,
            duration_ms=duration_ms,
        )
        logger.info(
            "report_finished request_id=%s status=ready dodo_requests=%s duration_ms=%s",
            request_id,
            dodo_requests,
            duration_ms,
        )
        return response

    def needs_units(self, plan: ReportPlan) -> bool:
        if plan.mode == PlanMode.METRICS:
            return True
        # DYNAMIC mode: check if the endpoint actually requires units
        return any(
            self.executor.profiles.resolve(endpoint).has_units
            for operation_id in plan.operation_ids
            if (endpoint := self.executor.repository.get(operation_id)) is not None
        )

    async def _run(self, plan: ReportPlan, unit_ids: list[str]) -> ReportResult:
        records_by_operation = await self.executor.execute(plan, unit_ids)
        return self.aggregator.aggregate(plan, records_by_operation)

    def _finish(
        self, request_id: str, started: float, status: Any, planner: Any, candidates: Any
    ) -> None:
        self.runs.finish(
            request_id,
            status=str(status),
            metric_ids=planner.plan.metric_ids,
            operation_ids=planner.plan.operation_ids,
            candidate_count=len(candidates),
            openai_input_tokens=planner.usage.input_tokens,
            openai_output_tokens=planner.usage.output_tokens,
            duration_ms=int((time.perf_counter() - started) * 1000),
        )

    def _export(
        self,
        request_id: str,
        _query: str,
        plan: Any,
        _unit_ids: list[str],
        columns: list[str],
        rows: list[dict[str, Any]],
        totals: dict[str, Any],
    ) -> dict[str, str] | None:
        if plan.output_format not in {OutputFormat.CSV, OutputFormat.XLSX}:
            return None
        report_id = uuid.uuid4().hex
        suffix = plan.output_format.value
        path = self.settings.reports_directory / f"{report_id}.{suffix}"
        period_label = period_label_from_dates(plan.date_from, plan.date_to)
        try:
            if plan.output_format == OutputFormat.CSV:
                export_csv(path, columns, rows, totals, period_label=period_label)
                media_type = "text/csv"
            else:
                export_xlsx(path, columns, rows, totals, period_label=period_label)
                media_type = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            self.files.add(report_id, request_id, path, media_type)
        except Exception:
            path.unlink(missing_ok=True)
            raise
        return {"report_id": report_id, "url": f"/api/reports/{report_id}/download"}
