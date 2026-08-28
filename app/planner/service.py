from __future__ import annotations

import json
from datetime import date
from typing import Any

from openai import AsyncOpenAI

from app.config import Settings
from app.documentation.models import EndpointCandidate
from app.dodo.units import Unit, UnitResolver
from app.errors import PlannerError
from app.planner.mock import build_mock_plan
from app.planner.prompts import SYSTEM_PROMPT
from app.planner.schemas import (
    PlanMode,
    PlannerResult,
    PlannerUsage,
    PlanStatus,
    ReportPlan,
)
from app.reports.metric_registry import MetricRegistry
from app.retrieval.normalizer import normalize_query


class PlannerService:
    def __init__(
        self,
        settings: Settings,
        registry: MetricRegistry,
        client: AsyncOpenAI | None = None,
        unit_resolver: UnitResolver | None = None,
    ) -> None:
        self.settings = settings
        self.registry = registry
        self.client = client
        self.unit_resolver = unit_resolver

    async def create_plan(
        self,
        query: str,
        candidates: list[EndpointCandidate],
        *,
        today: date | None = None,
    ) -> PlannerResult:
        current = today or date.today()
        if self.settings.planner_mock_mode:
            return build_mock_plan(
                query,
                self.registry,
                today=current,
                default_units=self.settings.default_unit_ids,
            )
        if not self.settings.openai_api_key and self.client is None:
            raise PlannerError(
                "Не задан OPENAI_API_KEY. Для локальной демонстрации "
                "включите PLANNER_MOCK_MODE=true"
            )
        client = self.client or AsyncOpenAI(api_key=self.settings.openai_api_key)
        recognized_units = (
            self.unit_resolver.recognize_in_query(query) if self.unit_resolver else []
        )
        context = {
            "current_date": current.isoformat(),
            "timezone": self.settings.app_timezone,
            "user_query": query,
            "recognized_units": [
                {"unit_name": unit.unit_name, "unit_id": unit.unit_id} for unit in recognized_units
            ],
            "metrics": {
                metric_id: {
                    "aliases": details["aliases"],
                    "operation_id": details["operation_id"],
                    "required_fields": details["required_fields"],
                    "granularities": details["granularities"],
                }
                for metric_id, details in self.registry.all().items()
            },
            "candidates": [
                {
                    "operation_id": item.operation_id,
                    "summary": item.compact_summary[:3000],
                    "response_fields": item.response_fields[:25],
                    "parameters": [
                        {
                            "name": parameter.name,
                            "in": parameter.location,
                            "required": parameter.required,
                            "type": parameter.type,
                            "format": parameter.format,
                            "enum": parameter.enum[:30],
                            "description": parameter.description[:500],
                        }
                        for parameter in item.parameters
                    ],
                }
                for item in candidates
            ],
        }
        try:
            model_name = self.settings.openai_model.lower()
            is_reasoning_model = any(tag in model_name for tag in ("gpt-5", "o1", "o3", "-r"))

            parse_kwargs: dict[str, Any] = {
                "model": self.settings.openai_model,
                "instructions": SYSTEM_PROMPT,
                "input": json.dumps(context, ensure_ascii=False),
                "text_format": ReportPlan,
                "max_output_tokens": max(self.settings.openai_max_output_tokens, 3000),
            }
            if is_reasoning_model:
                parse_kwargs["reasoning"] = {"effort": "minimal"}

            response = await client.responses.parse(**parse_kwargs)
            plan = response.output_parsed
            if plan is None:
                raise PlannerError("Модель не вернула структурированный план")
            self._apply_recognized_units(plan, recognized_units)
            self._prefer_single_registry_metric(plan, query)
            self._stabilize_unregistered_operation(plan)
            self._canonicalize_registry_fields(plan)
            usage: Any = getattr(response, "usage", None)
            return PlannerResult(
                plan=plan,
                usage=PlannerUsage(
                    input_tokens=int(getattr(usage, "input_tokens", 0) or 0),
                    output_tokens=int(getattr(usage, "output_tokens", 0) or 0),
                ),
            )
        except PlannerError:
            raise
        except Exception as exc:
            raise PlannerError(
                "Не удалось построить план отчёта", technical_message=str(exc)
            ) from exc

    @staticmethod
    def _apply_recognized_units(plan: ReportPlan, units: list[Unit]) -> None:
        if not units:
            return
        plan.unit_references = [unit.unit_id for unit in units]
        question = (plan.clarification_question or "").casefold()
        asks_for_unit = "завед" in question or "uuid" in question or "пиццер" in question
        has_complete_period = plan.date_from is not None and plan.date_to is not None
        if (
            plan.status == PlanStatus.NEEDS_CLARIFICATION
            and asks_for_unit
            and plan.metric_ids
            and has_complete_period
        ):
            plan.status = PlanStatus.READY
            plan.clarification_question = None

    def _canonicalize_registry_fields(self, plan: ReportPlan) -> None:
        # Dynamic plans choose an operation directly, so deriving operations from the
        # metric registry would erase the model's choice.
        if plan.mode == PlanMode.DYNAMIC:
            return
        if not plan.metric_ids or any(
            not self.registry.has(metric_id) for metric_id in plan.metric_ids
        ):
            return
        definitions = [self.registry.get(metric_id) for metric_id in plan.metric_ids]
        plan.operation_ids = list(
            dict.fromkeys(definition.operation_id for definition in definitions)
        )
        plan.selected_response_fields = list(
            dict.fromkeys(
                field for definition in definitions for field in definition.required_fields
            )
        )

    def _prefer_single_registry_metric(self, plan: ReportPlan, query: str) -> None:
        if plan.mode == PlanMode.METRICS and plan.metric_ids:
            return
        normalized = normalize_query(query)
        if any(
            marker in normalized
            for marker in ("деталь", "список", "записи", "сырые данные", "без агрегации")
        ):
            return
        matched = self.registry.match_aliases(normalized)
        if len(matched) != 1:
            return
        plan.mode = PlanMode.METRICS
        plan.metric_ids = matched
        plan.dynamic_aggregation = None
        plan.raw_collection = ""
        plan.operation_arguments = []

    @staticmethod
    def _stabilize_unregistered_operation(plan: ReportPlan) -> None:
        if plan.mode != PlanMode.DYNAMIC:
            return
        collection = plan.dynamic_aggregation.collection if plan.dynamic_aggregation else ""
        plan.mode = PlanMode.RAW
        plan.dynamic_aggregation = None
        plan.raw_collection = collection.replace("[]", "").strip(".")
        plan.selected_response_fields = []
