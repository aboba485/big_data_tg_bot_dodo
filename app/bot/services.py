from __future__ import annotations

import asyncio
import copy
import hashlib
import logging
import re
import time
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from app.bot.catalog import ReportCatalog, ReportCategory
from app.bot.errors import BotAccessError, BotBusyError, BotInputError
from app.bot.messages.texts import DRIVE_NOT_CONFIGURED, DRIVE_NOT_LINKED
from app.bot.models import BotPreparation, BotReportResult
from app.config import Settings
from app.documentation.models import EndpointCandidate
from app.dodo.channels import (
    DEFAULT_SALES_CHANNELS,
    SALES_CHANNEL_FILTER,
    SALES_CHANNEL_GROUP,
    canonical_sales_channel,
    sales_channel_label,
)
from app.dodo.units import UNIT_RE, UnitResolver
from app.errors import GoogleDriveError, UnitResolutionError
from app.google_drive.service import GoogleDriveService
from app.planner.schemas import (
    Granularity,
    OutputFormat,
    PlanMode,
    PlannerResult,
    PlanStatus,
    ReportPlan,
)
from app.planner.service import PlannerService
from app.planner.validator import ReportPlanValidator
from app.report_service import ReportOrchestrator
from app.reports.exporters import export_csv
from app.reports.matrix import (
    TOTAL_LABEL,
    build_matrix,
    is_total_label,
    period_label_from_dates,
)
from app.reports.metric_registry import MetricRegistry
from app.retrieval.normalizer import normalize_query
from app.retrieval.service import RetrievalService
from app.storage.generated_files import GeneratedFileRepository
from app.users.models import TelegramUser
from app.users.service import UserAccessService

INJECTION_MARKERS = (
    "ignore previous",
    "ignore all",
    "игнорируй предыдущ",
    "системный промпт",
    "system prompt",
    "покажи переменные окружения",
    "show environment",
    "прочитай файл",
    "read file",
)
SQL_MARKER = re.compile(
    r"(?:^|[\s;])(?:select|insert|update|delete|drop|alter|pragma|attach)\s+",
    re.IGNORECASE,
)
UUID_RE = re.compile(
    r"\b[0-9a-fA-F]{32}\b|\b[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}\b"
)
ALL_UNITS_PHRASE_RE = re.compile(r"\b(?:все|всем|всех)\s+(?:заведени\w*|ресторан\w*|пиццери\w*)\b")
logger = logging.getLogger(__name__)
MAX_PREPARED_CONTEXTS = 256


@dataclass
class PreparationContext:
    public: BotPreparation
    plan: ReportPlan | None = None
    planner_result: PlannerResult | None = None
    candidates: list[EndpointCandidate] = field(default_factory=list)
    unit_ids: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class UnitCity:
    city_id: str
    label: str
    units: tuple[tuple[str, str], ...]


class BotReportService:
    def __init__(
        self,
        settings: Settings,
        orchestrator: ReportOrchestrator,
        retrieval: RetrievalService,
        planner: PlannerService,
        validator: ReportPlanValidator,
        metrics: MetricRegistry,
        resolver: UnitResolver,
        files: GeneratedFileRepository,
        user_access: UserAccessService,
        google_drive: GoogleDriveService | None = None,
    ) -> None:
        self.settings = settings
        self.google_drive = google_drive
        self.orchestrator = orchestrator
        self.retrieval = retrieval
        self.planner = planner
        self.validator = validator
        self.metrics = metrics
        self.resolver = resolver
        self.files = files
        self.user_access = user_access
        self.catalog = ReportCatalog(metrics)
        self._user_limits: defaultdict[int, asyncio.Semaphore] = defaultdict(
            lambda: asyncio.Semaphore(settings.telegram_max_concurrent_reports_per_user)
        )
        self._in_flight: defaultdict[int, set[str]] = defaultdict(set)
        self._prepared_contexts: dict[tuple[int, str], tuple[float, PreparationContext]] = {}

    def available_reports(self, user: TelegramUser) -> list[tuple[str, str]]:
        result = []
        aliases = self.metrics.aliases()
        for metric_id in sorted(self.metrics.all()):
            if user.can_access_report(metric_id):
                label = aliases.get(metric_id, [metric_id])[0]
                result.append((metric_id, label))
        return result

    def available_units(self, user: TelegramUser) -> list[tuple[str, str]]:
        names = self.resolver.names()
        if user.role.value == "admin" or "*" in user.allowed_unit_ids:
            unit_ids = list(names) or list(self.settings.default_unit_ids)
        else:
            unit_ids = list(user.allowed_unit_ids)
        unit_ids = [unit_id for unit_id in dict.fromkeys(unit_ids) if UNIT_RE.fullmatch(unit_id)]
        return [(unit_id, names.get(unit_id) or f"Заведение {unit_id[:8]}") for unit_id in unit_ids]

    def available_unit_cities(self, user: TelegramUser) -> list[UnitCity]:
        grouped: defaultdict[str, list[tuple[str, str]]] = defaultdict(list)
        labels: dict[str, str] = {}
        for unit_id, unit_label in self.available_units(user):
            city_label = self._city_label(unit_label)
            city_key = city_label.casefold()
            labels[city_key] = city_label
            grouped[city_key].append((unit_id, self._display_label(unit_label)))
        result = []
        for city_key in sorted(grouped, key=self._natural_key):
            units = tuple(sorted(grouped[city_key], key=lambda item: self._natural_key(item[1])))
            city_id = hashlib.sha256(city_key.encode()).hexdigest()[:12]
            result.append(UnitCity(city_id=city_id, label=labels[city_key], units=units))
        return result

    def units_in_city(self, user: TelegramUser, city_id: str) -> list[tuple[str, str]]:
        city = next(
            (item for item in self.available_unit_cities(user) if item.city_id == city_id),
            None,
        )
        return list(city.units) if city else []

    def available_categories(self, user: TelegramUser) -> list[ReportCategory]:
        return self.catalog.categories_for(user)

    def sheets_available(self, user: TelegramUser) -> bool:
        return self.google_drive is not None and self.google_drive.is_linked(user.telegram_id)

    def _ensure_sheets_available(self, user: TelegramUser, output_format: OutputFormat) -> None:
        if output_format != OutputFormat.SHEETS:
            return
        if self.google_drive is None or not self.google_drive.enabled:
            raise BotInputError(DRIVE_NOT_CONFIGURED)
        if not self.google_drive.is_linked(user.telegram_id):
            raise BotInputError(DRIVE_NOT_LINKED)

    def reports_in_category(self, category_id: str, user: TelegramUser) -> list[tuple[str, str]]:
        return self.catalog.reports_for(category_id, user)

    @staticmethod
    def _city_label(unit_label: str) -> str:
        match = re.fullmatch(r"\s*(.+?)[\s-]+\d.*", unit_label)
        if not match or unit_label.startswith("Заведение "):
            return "Без города"
        return BotReportService._display_label(match.group(1))

    @staticmethod
    def _display_label(value: str) -> str:
        value = value.strip()
        return value[:1].upper() + value[1:] if value else value

    @staticmethod
    def _natural_key(value: str) -> tuple[object, ...]:
        return tuple(
            int(part) if part.isdigit() else part.casefold() for part in re.split(r"(\d+)", value)
        )

    def validate_query(self, query: str) -> str:
        value = query.strip()
        if not 3 <= len(value) <= 4000:
            raise BotInputError("Запрос должен содержать от 3 до 4000 символов.")
        normalized = normalize_query(value)
        if any(marker in normalized for marker in INJECTION_MARKERS) or SQL_MARKER.search(value):
            raise BotInputError("Запрос содержит недопустимые инструкции.")
        return value

    async def prepare(
        self,
        user: TelegramUser,
        query: str,
        output_format: OutputFormat | None = None,
        *,
        defer_units: bool = False,
    ) -> BotPreparation:
        context = await self._prepare_context(user, query, output_format, defer_units=defer_units)
        if defer_units and context.public.status == "ready":
            self._store_prepared_context(user.telegram_id, query, context)
        return context.public

    async def _prepare_context(
        self,
        user: TelegramUser,
        query: str,
        output_format: OutputFormat | None = None,
        *,
        defer_units: bool = False,
        unit_ids_override: list[str] | None = None,
        granularity_override: Granularity | None = None,
        sales_channel_choice_override: str | None = None,
    ) -> PreparationContext:
        query = self.validate_query(query)
        matched_reports = self.metrics.match_aliases(normalize_query(query))
        if matched_reports:
            try:
                self.user_access.ensure_report_access(user, matched_reports)
            except PermissionError as exc:
                raise BotAccessError(str(exc)) from exc

        mentioned_units = self._mentioned_unit_ids(query)
        try:
            self.user_access.ensure_unit_access(user, mentioned_units)
        except PermissionError as exc:
            raise BotAccessError(str(exc)) from exc
        has_all_units_phrase = bool(ALL_UNITS_PHRASE_RE.search(normalize_query(query)))
        mentioned_city_units = []
        if defer_units and not mentioned_units and has_all_units_phrase:
            mentioned_city_units = self._mentioned_city_unit_ids(user, query)

        candidates = await self.retrieval.search_endpoints(query)
        planner_result = await self.planner.create_plan(query, candidates)
        plan = planner_result.plan
        self.validator.apply_query_intent(plan, query)
        if sales_channel_choice_override is not None and plan.status == PlanStatus.READY:
            self.validator.apply_sales_channel_choice(plan, sales_channel_choice_override)
        planned_report_types = (
            plan.metric_ids if plan.mode == PlanMode.METRICS else plan.operation_ids
        )
        try:
            self.user_access.ensure_report_access(user, planned_report_types)
        except PermissionError as exc:
            raise BotAccessError(str(exc)) from exc
        if output_format is not None:
            plan.output_format = output_format
        if plan.status == PlanStatus.NEEDS_CLARIFICATION:
            question = (plan.clarification_question or "").casefold()
            asks_for_unit = any(
                marker in question for marker in ("завед", "uuid", "пиццер", "подраздел")
            )
            can_defer_units = (
                asks_for_unit
                and planned_report_types
                and plan.date_from is not None
                and plan.date_to is not None
                and (defer_units or bool(unit_ids_override))
            )
            if can_defer_units:
                plan.status = PlanStatus.READY
                plan.clarification_question = None
                if plan.mode == PlanMode.METRICS and plan.metric_ids:
                    plan.operation_ids = list(
                        dict.fromkeys(
                            self.metrics.get(metric_id).operation_id
                            for metric_id in plan.metric_ids
                            if self.metrics.has(metric_id)
                        )
                    )
            else:
                return PreparationContext(
                    public=BotPreparation(
                        status="needs_clarification",
                        question=plan.clarification_question or "Уточните параметры отчёта.",
                        report_types=planned_report_types,
                        date_from=plan.date_from,
                        date_to=plan.date_to,
                    ),
                    plan=plan,
                    planner_result=planner_result,
                    candidates=candidates,
                )
        if plan.status == PlanStatus.UNSUPPORTED:
            return PreparationContext(
                public=BotPreparation(
                    status="unsupported",
                    reason=plan.unsupported_reason or "Этот отчёт не поддерживается.",
                ),
                plan=plan,
                planner_result=planner_result,
                candidates=candidates,
            )

        unit_ids: list[str] = []
        if unit_ids_override is not None:
            unit_ids = list(dict.fromkeys(unit_ids_override))
            if not unit_ids:
                raise BotInputError("Не выбраны заведения.")
            plan.unit_references = unit_ids
        elif mentioned_units:
            unit_ids = mentioned_units
            plan.unit_references = unit_ids
        elif mentioned_city_units:
            unit_ids = mentioned_city_units
            plan.unit_references = unit_ids
        elif defer_units and has_all_units_phrase:
            plan.unit_references = []
            unit_ids = []
        elif plan.unit_references or self.orchestrator.needs_units(plan):
            try:
                resolution = self.resolver.resolve(plan.unit_references)
            except UnitResolutionError:
                if not defer_units:
                    raise
                plan.unit_references = []
                unit_ids = []
            else:
                if resolution.status != "ready":
                    if not defer_units:
                        return PreparationContext(
                            public=BotPreparation(
                                status="needs_clarification",
                                question=resolution.question
                                or "По какому подразделению нужен отчёт?",
                                report_types=planned_report_types,
                                date_from=plan.date_from,
                                date_to=plan.date_to,
                            ),
                            plan=plan,
                            planner_result=planner_result,
                            candidates=candidates,
                        )
                    unit_ids = []
                else:
                    unit_ids = resolution.unit_ids

        if granularity_override is not None:
            plan.granularity = granularity_override
            plan.group_by = [
                item
                for item in plan.group_by
                if item not in {"day", "week", "month", "hour", "total"}
            ]
            if granularity_override != Granularity.TOTAL:
                plan.group_by = [granularity_override.value, *plan.group_by]
            for metric_id in plan.metric_ids:
                definition = self.metrics.get(metric_id)
                if plan.granularity.value not in definition.granularities:
                    raise BotInputError("Эта детализация недоступна для выбранного отчёта.")

        if unit_ids and "unit" not in plan.group_by:
            plan.group_by = [*plan.group_by, "unit"]

        if unit_ids or not defer_units:
            self.validator.validate(plan, candidates, unit_ids)
        else:
            # Units are selected later via buttons; validate the plan with a placeholder.
            self.validator.validate(plan, candidates, ["00000000000000000000000000000000"])
        if plan.status == PlanStatus.NEEDS_CLARIFICATION:
            return PreparationContext(
                public=BotPreparation(
                    status="needs_clarification",
                    question=plan.clarification_question or "Уточните параметры отчёта.",
                    report_types=planned_report_types,
                    unit_ids=unit_ids,
                    date_from=plan.date_from,
                    date_to=plan.date_to,
                ),
                plan=plan,
                planner_result=planner_result,
                candidates=candidates,
                unit_ids=unit_ids,
            )
        if plan.date_from is not None and plan.date_to is not None:
            self.validate_period(plan.date_from, plan.date_to)
        elif plan.date_from is not None or plan.date_to is not None:
            return PreparationContext(
                public=BotPreparation(
                    status="needs_clarification",
                    question="За какой период нужен отчёт?",
                    report_types=planned_report_types,
                    unit_ids=unit_ids,
                    date_from=plan.date_from,
                    date_to=plan.date_to,
                ),
                plan=plan,
                planner_result=planner_result,
                candidates=candidates,
                unit_ids=unit_ids,
            )
        try:
            self.user_access.ensure_report_access(user, planned_report_types)
            if unit_ids:
                self.user_access.ensure_unit_access(user, unit_ids)
        except PermissionError as exc:
            raise BotAccessError(str(exc)) from exc
        return PreparationContext(
            public=BotPreparation(
                status="ready",
                report_types=planned_report_types,
                unit_ids=unit_ids,
                date_from=plan.date_from,
                date_to=plan.date_to,
                **self._sales_channel_preparation(plan),
            ),
            plan=plan,
            planner_result=planner_result,
            candidates=candidates,
            unit_ids=unit_ids,
        )

    async def run(
        self,
        user: TelegramUser,
        query: str,
        output_format: OutputFormat,
        unit_ids: list[str] | None = None,
        granularity: Granularity | None = None,
        sales_channel_choice: str | None = None,
    ) -> BotReportResult:
        self._ensure_sheets_available(user, output_format)
        selected_units = list(dict.fromkeys(unit_ids or []))
        granularity_value = granularity.value if granularity is not None else ""
        fingerprint = hashlib.sha256(
            f"{query}\0{output_format.value}\0{','.join(sorted(selected_units))}\0"
            f"{granularity_value}\0{sales_channel_choice or ''}".encode()
        ).hexdigest()
        limit = self._user_limits[user.telegram_id]
        if fingerprint in self._in_flight[user.telegram_id] or limit.locked():
            raise BotBusyError("Дождитесь завершения предыдущего отчёта.")
        started = time.perf_counter()
        final_status = "error"
        report_types: list[str] = []
        async with limit:
            self._in_flight[user.telegram_id].add(fingerprint)
            try:
                context = self._take_prepared_context(user.telegram_id, query)
                if context is not None and selected_units:
                    context = self._apply_cached_overrides(
                        user,
                        context,
                        output_format,
                        selected_units,
                        granularity,
                        sales_channel_choice,
                    )
                else:
                    context = await self._prepare_context(
                        user,
                        query,
                        output_format,
                        unit_ids_override=selected_units or None,
                        granularity_override=granularity,
                        sales_channel_choice_override=sales_channel_choice,
                    )
                preparation = context.public
                report_types = preparation.report_types
                if preparation.status != "ready":
                    final_status = preparation.status
                    return BotReportResult(
                        status=preparation.status,
                        question=preparation.question,
                        reason=preparation.reason,
                    )
                logger.info(
                    "telegram_report_started telegram_id=%s report_types=%s period=%s:%s",
                    user.telegram_id,
                    preparation.report_types,
                    preparation.date_from,
                    preparation.date_to,
                )
                try:
                    async with asyncio.timeout(self.settings.telegram_report_timeout_seconds):
                        if context.plan is None or context.planner_result is None:
                            raise BotInputError("Подготовленный план отчёта отсутствует.")
                        current_user = self.user_access.authorized(user.telegram_id)
                        if current_user is None:
                            raise BotAccessError("Доступ к боту отозван.")
                        try:
                            self.user_access.ensure_report_access(
                                current_user, preparation.report_types
                            )
                            self.user_access.ensure_unit_access(current_user, context.unit_ids)
                        except PermissionError as exc:
                            raise BotAccessError(str(exc)) from exc
                        response = await self.orchestrator.create_prepared_report(
                            query,
                            context.plan,
                            context.unit_ids,
                            context.planner_result,
                            context.candidates,
                        )
                except TimeoutError as exc:
                    raise BotBusyError("Формирование отчёта превысило допустимое время.") from exc
                result = await self._result_from_response(
                    response, output_format=output_format, telegram_id=user.telegram_id
                )
                final_status = result.status
                return result
            finally:
                self._in_flight[user.telegram_id].discard(fingerprint)
                logger.info(
                    "telegram_report_finished telegram_id=%s report_types=%s status=%s "
                    "duration_ms=%s",
                    user.telegram_id,
                    report_types,
                    final_status,
                    int((time.perf_counter() - started) * 1000),
                )

    async def run_selected(
        self,
        user: TelegramUser,
        *,
        metric_id: str,
        unit_ids: list[str],
        date_from: date,
        date_to: date,
        output_format: OutputFormat,
        granularity: Granularity = Granularity.TOTAL,
        sales_channel_choice: str | None = None,
    ) -> BotReportResult:
        if not self.metrics.has(metric_id):
            raise BotInputError("Неизвестный тип отчёта.")
        if not unit_ids:
            raise BotInputError("Не выбраны заведения.")
        self._ensure_sheets_available(user, output_format)
        unit_ids = list(dict.fromkeys(unit_ids))
        self.validate_period(date_from, date_to)
        try:
            self.user_access.ensure_report_access(user, [metric_id])
            self.user_access.ensure_unit_access(user, unit_ids)
        except PermissionError as exc:
            raise BotAccessError(str(exc)) from exc
        definition = self.metrics.get(metric_id)
        if granularity.value not in definition.granularities:
            raise BotInputError("Эта детализация недоступна для выбранного отчёта.")
        endpoint = self.retrieval.repository.get(definition.operation_id)
        if endpoint is None:
            raise BotInputError("Документация для выбранного отчёта не найдена.")
        group_by = ["unit"]
        if granularity != Granularity.TOTAL:
            group_by = [granularity.value, *group_by]
        if "sales channel" in definition.groups:
            group_by.append("sales channel")
        plan = ReportPlan(
            status=PlanStatus.READY,
            mode=PlanMode.METRICS,
            metric_ids=[metric_id],
            operation_ids=[definition.operation_id],
            date_from=date_from,
            date_to=date_to,
            unit_references=unit_ids,
            granularity=granularity,
            group_by=group_by,
            output_format=output_format,
        )
        if sales_channel_choice is not None:
            self.validator.apply_sales_channel_choice(plan, sales_channel_choice)
        candidate = EndpointCandidate(
            operation_id=endpoint.operation_id,
            score=1.0,
            compact_summary=endpoint.compact_summary,
        )
        self.validator.validate(plan, [candidate], unit_ids)
        fingerprint = hashlib.sha256(
            f"selected\0{metric_id}\0{','.join(sorted(unit_ids))}\0{date_from}\0{date_to}\0"
            f"{granularity.value}\0{output_format.value}\0{sales_channel_choice or ''}".encode()
        ).hexdigest()
        limit = self._user_limits[user.telegram_id]
        if fingerprint in self._in_flight[user.telegram_id] or limit.locked():
            raise BotBusyError("Дождитесь завершения предыдущего отчёта.")
        async with limit:
            self._in_flight[user.telegram_id].add(fingerprint)
            try:
                current_user = self.user_access.authorized(user.telegram_id)
                if current_user is None:
                    raise BotAccessError("Доступ к боту отозван.")
                try:
                    self.user_access.ensure_report_access(current_user, [metric_id])
                    self.user_access.ensure_unit_access(current_user, unit_ids)
                except PermissionError as exc:
                    raise BotAccessError(str(exc)) from exc
                query = f"Кнопочный отчёт {metric_id} за {date_from}—{date_to}"
                try:
                    async with asyncio.timeout(self.settings.telegram_report_timeout_seconds):
                        response = await self.orchestrator.create_prepared_report(
                            query, plan, unit_ids, PlannerResult(plan=plan), [candidate]
                        )
                except TimeoutError as exc:
                    raise BotBusyError("Формирование отчёта превысило допустимое время.") from exc
                return await self._result_from_response(
                    response, output_format=output_format, telegram_id=user.telegram_id
                )
            finally:
                self._in_flight[user.telegram_id].discard(fingerprint)

    async def _result_from_response(
        self,
        response: dict[str, Any],
        *,
        output_format: OutputFormat,
        telegram_id: int,
    ) -> BotReportResult:
        status = str(response.get("status", "unsupported"))
        if status != "ready":
            return BotReportResult(
                status=status,
                question=response.get("question"),
                reason=response.get("reason"),
                response=response,
            )
        full_text = self._format_response_text(response)
        text = self._truncate_response(full_text)
        if output_format == OutputFormat.SHEETS:
            return await self._sheet_result(response, full_text, telegram_id)
        download = response.get("download")
        if not download and (
            len(response.get("rows") or []) > self.settings.telegram_max_message_rows
            or len(full_text) > 4000
        ):
            download = self._create_overflow_csv(response)
        if not download:
            return BotReportResult(status="ready", text=text, response=response)
        report_id = str(download.get("report_id", ""))
        file_path, _media_type = self._resolve_file(report_id)
        return BotReportResult(
            status="ready",
            text=text,
            report_id=report_id,
            file_path=file_path,
            file_name=file_path.name,
            response=response,
        )

    async def _sheet_result(
        self, response: dict[str, Any], full_text: str, telegram_id: int
    ) -> BotReportResult:
        if self.google_drive is None or not self.google_drive.enabled:
            raise BotInputError(DRIVE_NOT_CONFIGURED)
        try:
            url, _spreadsheet_id = await self.google_drive.create_spreadsheet(
                telegram_id,
                title=self._sheet_title(response),
                columns=list(response.get("columns") or []),
                rows=list(response.get("rows") or []),
                totals=dict(response.get("totals") or {}),
                period_label=self._period_label(response),
            )
        except GoogleDriveError as exc:
            raise BotInputError(exc.message) from exc
        return BotReportResult(
            status="ready",
            text=self._truncate_response(f"Google Sheets: {url}\n\n{full_text}"),
            sheet_url=url,
            response=response,
        )

    @staticmethod
    def _sheet_title(response: dict[str, Any]) -> str:
        plan = response.get("plan") or {}
        names = list(plan.get("metrics") or []) or list(plan.get("operations") or [])
        label = ", ".join(str(name) for name in names) or "Отчёт Dodo IS"
        date_from = plan.get("date_from")
        date_to = plan.get("date_to")
        if date_from and date_to:
            label = f"{label} {date_from} — {date_to}"
        return label[:200]

    @staticmethod
    def _period_label(response: dict[str, Any]) -> str | None:
        plan = response.get("plan") or {}
        return period_label_from_dates(plan.get("date_from"), plan.get("date_to"))

    def cleanup_file(self, report_id: str) -> None:
        try:
            found = self.files.get(report_id)
            if found:
                path, _media_type = found
                try:
                    path.resolve().relative_to(self.settings.reports_directory.resolve())
                except ValueError:
                    path = Path()
                if path and path.is_file():
                    path.unlink()
        finally:
            self.files.delete(report_id)

    def validate_period(self, date_from: date, date_to: date) -> None:
        if date_from > date_to:
            raise BotInputError("Дата начала должна быть не позже даты окончания.")
        days = (date_to - date_from).days + 1
        if days > self.settings.telegram_max_report_days:
            raise BotInputError(
                f"Период не должен превышать {self.settings.telegram_max_report_days} дней."
            )

    def format_response(self, response: dict[str, Any]) -> str:
        return self._truncate_response(self._format_response_text(response))

    def _format_response_text(self, response: dict[str, Any]) -> str:
        summary = str(response.get("summary") or "Отчёт сформирован.")
        plan = response.get("plan") or {}
        channel_values = [
            str(value)
            for report_filter in plan.get("filters") or []
            if report_filter.get("name") == SALES_CHANNEL_FILTER
            for value in report_filter.get("values") or []
        ]
        if channel_values:
            labels = ", ".join(sales_channel_label(value) for value in channel_values)
            summary = f"{summary}\nКанал продаж: {labels}."
        rows = response.get("rows") or []
        columns = response.get("columns") or []
        totals = response.get("totals") or {}
        if not rows or not columns:
            return summary
        matrix = build_matrix(
            list(columns),
            list(rows),
            dict(totals),
            period_label=self._period_label(response),
        )
        if matrix is not None:
            return self._format_matrix_text(summary, matrix)
        lines = [summary, ""]
        limit = self.settings.telegram_max_message_rows
        dimension_columns = {
            "unitId",
            "unitName",
            "unit_id",
            "unit_name",
            "salesChannel",
            "ingredient",
            "ingredientCategory",
            "stopReason",
            "day",
            "week",
            "month",
            "hour",
        }
        metric_columns = [column for column in columns if column not in dimension_columns]
        for row in rows[:limit]:
            location = (
                str(row.get("unitName") or row.get("unit_name") or "").strip()
                or str(row.get("unitId") or row.get("unit_id") or "").strip()
                or "Заведение"
            )
            time_bucket = next(
                (
                    str(row.get(key)).strip()
                    for key in ("hour", "day", "week", "month")
                    if str(row.get(key) or "").strip()
                ),
                "",
            )
            if time_bucket:
                location = f"{location} / {time_bucket}"
            channel = str(row.get("salesChannel") or "").strip()
            if channel:
                location = f"{location} / {channel}"
            if metric_columns:
                values = ", ".join(f"{column}={row.get(column, '')}" for column in metric_columns)
                lines.append(f"{location}: {values}")
            else:
                values = [f"{column}: {row.get(column, '')}" for column in columns]
                lines.append(" | ".join(values))
        if len(rows) > limit:
            lines.append(f"… ещё строк: {len(rows) - limit}")
        if totals:
            if metric_columns:
                total_parts = [
                    f"{key}={value}" for key, value in totals.items() if key in metric_columns
                ]
            else:
                total_parts = [f"{key}={value}" for key, value in totals.items()]
            if not total_parts:
                total_parts = [f"{key}={value}" for key, value in totals.items()]
            lines.extend(["", f"Итого: {', '.join(total_parts)}"])
        return "\n".join(lines)

    def _format_matrix_text(self, summary: str, matrix: list[list[Any]]) -> str:
        header = matrix[0] if matrix else []
        dates = header[1:]
        lines = [summary]
        if dates:
            lines.append(" | ".join(str(item) for item in dates))
        limit = self.settings.telegram_max_message_rows
        body = [row for row in matrix[1:] if row and not is_total_label(row[0])]
        totals = [row for row in matrix[1:] if row and is_total_label(row[0])]
        for row in body[:limit]:
            values = " | ".join("" if item is None else str(item) for item in row[1:])
            lines.append(f"{row[0]}: {values}")
        if len(body) > limit:
            lines.append(f"… ещё строк: {len(body) - limit}")
        for row in totals:
            values = " | ".join("" if item is None else str(item) for item in row[1:])
            label = row[0] or TOTAL_LABEL
            lines.append(f"{label}: {values}")
        return "\n".join(lines)

    @staticmethod
    def _truncate_response(value: str) -> str:
        if len(value) <= 4000:
            return value
        notice = "\n… Полный результат приложен файлом."
        return value[: 4000 - len(notice)] + notice

    def _mentioned_unit_ids(self, query: str) -> list[str]:
        found = list(UUID_RE.findall(query))
        found.extend(unit.unit_id for unit in self.resolver.recognize_in_query(query))
        return list(dict.fromkeys(found))

    def _mentioned_city_unit_ids(self, user: TelegramUser, query: str) -> list[str]:
        normalized = normalize_query(query)
        if not ALL_UNITS_PHRASE_RE.search(normalized):
            return []
        padded_query = f" {normalized} "
        result: list[str] = []
        for city in self.available_unit_cities(user):
            variants = self._city_query_variants(normalize_query(city.label))
            if any(f" {variant} " in padded_query for variant in variants):
                result.extend(unit_id for unit_id, _label in city.units)
        return list(dict.fromkeys(result))

    @staticmethod
    def _city_query_variants(city: str) -> set[str]:
        variants = {city}
        if city.endswith("а"):
            variants.update({f"{city[:-1]}ы", f"{city[:-1]}е"})
        elif city.endswith("я"):
            variants.update({f"{city[:-1]}и", f"{city[:-1]}е"})
        elif city.endswith("ь"):
            variants.add(f"{city[:-1]}и")
        elif city and city[-1].isalpha():
            variants.update({f"{city}а", f"{city}е"})
        return variants

    @staticmethod
    def _prepared_key(telegram_id: int, query: str) -> tuple[int, str]:
        digest = hashlib.sha256(query.strip().encode()).hexdigest()
        return telegram_id, digest

    def _store_prepared_context(
        self, telegram_id: int, query: str, context: PreparationContext
    ) -> None:
        now = time.monotonic()
        expired = [
            key
            for key, (expires_at, _context) in self._prepared_contexts.items()
            if expires_at <= now
        ]
        for key in expired:
            self._prepared_contexts.pop(key, None)
        while len(self._prepared_contexts) >= MAX_PREPARED_CONTEXTS:
            self._prepared_contexts.pop(next(iter(self._prepared_contexts)))
        expires_at = now + self.settings.telegram_fsm_ttl_seconds
        self._prepared_contexts[self._prepared_key(telegram_id, query)] = (
            expires_at,
            copy.deepcopy(context),
        )

    def _take_prepared_context(self, telegram_id: int, query: str) -> PreparationContext | None:
        cached = self._prepared_contexts.pop(self._prepared_key(telegram_id, query), None)
        if cached is None:
            return None
        expires_at, context = cached
        if time.monotonic() >= expires_at:
            return None
        return context

    def _apply_cached_overrides(
        self,
        user: TelegramUser,
        context: PreparationContext,
        output_format: OutputFormat,
        unit_ids: list[str],
        granularity: Granularity | None,
        sales_channel_choice: str | None,
    ) -> PreparationContext:
        plan = context.plan
        if plan is None or context.planner_result is None:
            raise BotInputError("Подготовленный план отчёта отсутствует.")
        plan.output_format = output_format
        plan.unit_references = unit_ids
        if granularity is not None:
            plan.granularity = granularity
            plan.group_by = [
                item
                for item in plan.group_by
                if item not in {"day", "week", "month", "hour", "total"}
            ]
            if granularity != Granularity.TOTAL:
                plan.group_by = [granularity.value, *plan.group_by]
            for metric_id in plan.metric_ids:
                definition = self.metrics.get(metric_id)
                if granularity.value not in definition.granularities:
                    raise BotInputError("Эта детализация недоступна для выбранного отчёта.")
        if "unit" not in plan.group_by:
            plan.group_by = [*plan.group_by, "unit"]
        if sales_channel_choice is not None:
            self.validator.apply_sales_channel_choice(plan, sales_channel_choice)
        self.validator.validate(plan, context.candidates, unit_ids)
        report_types = plan.metric_ids if plan.mode == PlanMode.METRICS else plan.operation_ids
        try:
            self.user_access.ensure_report_access(user, report_types)
            self.user_access.ensure_unit_access(user, unit_ids)
        except PermissionError as exc:
            raise BotAccessError(str(exc)) from exc
        if plan.date_from is not None and plan.date_to is not None:
            self.validate_period(plan.date_from, plan.date_to)
        context.unit_ids = unit_ids
        context.public = BotPreparation(
            status="ready",
            report_types=report_types,
            unit_ids=unit_ids,
            date_from=plan.date_from,
            date_to=plan.date_to,
            **self._sales_channel_preparation(plan),
        )
        context.planner_result.plan = plan
        return context

    def _sales_channel_preparation(self, plan: ReportPlan) -> dict[str, Any]:
        capability = self.validator.sales_channel_capability(plan)
        if capability is None:
            return {}
        selected = next(
            (
                value
                for report_filter in plan.filters
                if report_filter.name == SALES_CHANNEL_FILTER
                for value in report_filter.values
            ),
            "split" if SALES_CHANNEL_GROUP in plan.group_by else "",
        )
        suggested = [
            value
            for default in DEFAULT_SALES_CHANNELS
            if (value := canonical_sales_channel(default, capability.values)) is not None
        ]
        suggested.extend(value for value in capability.values if value not in suggested)
        return {
            "sales_channel_options": suggested,
            "sales_channel_can_split": capability.can_group,
            "sales_channel_selection": selected,
        }
    def _resolve_file(self, report_id: str) -> tuple[Path, str]:
        if not re.fullmatch(r"[0-9a-f]{32}", report_id):
            raise BotInputError("Некорректный идентификатор файла отчёта.")
        found = self.files.get(report_id)
        if not found:
            raise BotInputError("Файл отчёта не найден.")
        path, media_type = found
        try:
            path.resolve().relative_to(self.settings.reports_directory.resolve())
        except ValueError as exc:
            raise BotInputError("Файл отчёта находится вне разрешённой директории.") from exc
        if not path.is_file():
            raise BotInputError("Файл отчёта не найден.")
        return path, media_type

    def _create_overflow_csv(self, response: dict[str, Any]) -> dict[str, str]:
        report_id = uuid.uuid4().hex
        path = self.settings.reports_directory / f"{report_id}.csv"
        try:
            export_csv(
                path,
                list(response.get("columns") or []),
                list(response.get("rows") or []),
                dict(response.get("totals") or {}),
                period_label=self._period_label(response),
            )
            self.files.add(
                report_id,
                str(response["request_id"]),
                path,
                "text/csv",
            )
        except Exception:
            path.unlink(missing_ok=True)
            raise
        return {"report_id": report_id}
