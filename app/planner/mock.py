from __future__ import annotations

import calendar
import re
from datetime import date, timedelta

from app.dodo.channels import SALES_CHANNEL_GROUP, sales_channel_intent
from app.planner.schemas import (
    Granularity,
    OutputFormat,
    PlannerResult,
    PlanStatus,
    ReportFilter,
    ReportPlan,
)
from app.reports.metric_registry import MetricRegistry
from app.retrieval.normalizer import normalize_query

UUID_RE = re.compile(
    r"\b[0-9a-fA-F]{32}\b|\b[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}\b"
)
MONTHS = {
    "январ": 1,
    "феврал": 2,
    "март": 3,
    "апрел": 4,
    "ма": 5,
    "июн": 6,
    "июл": 7,
    "август": 8,
    "сентябр": 9,
    "октябр": 10,
    "ноябр": 11,
    "декабр": 12,
}


def _period(query: str, today: date) -> tuple[date | None, date | None]:
    normalized = normalize_query(query)
    if "прошлую неделю" in normalized or "прошлой недел" in normalized:
        this_monday = today - timedelta(days=today.weekday())
        return this_monday - timedelta(days=7), this_monday - timedelta(days=1)
    iso_dates = re.findall(r"\b(\d{4})-(\d{2})-(\d{2})\b", normalized)
    if len(iso_dates) >= 2:
        values = [date(*map(int, item)) for item in iso_dates[:2]]
        return values[0], values[1]
    year_match = re.search(r"\b(20\d{2})\b", normalized)
    if year_match:
        year = int(year_match.group(1))
        month = next(
            (number for root, number in MONTHS.items() if re.search(rf"\b{root}\w*", normalized)),
            None,
        )
        if month:
            day_range = re.search(r"\b(?:с\s+)?(\d{1,2})\s+по\s+(\d{1,2})\b", normalized)
            if day_range:
                return (
                    date(year, month, int(day_range.group(1))),
                    date(year, month, int(day_range.group(2))),
                )
            return date(year, month, 1), date(year, month, calendar.monthrange(year, month)[1])
    last_days = re.search(r"последн\w*\s+(\d+)\s+д", normalized)
    if last_days:
        count = int(last_days.group(1))
        return today - timedelta(days=count - 1), today
    return None, None


def build_mock_plan(
    query: str,
    registry: MetricRegistry,
    *,
    today: date,
    default_units: list[str],
) -> PlannerResult:
    normalized = normalize_query(query)
    metrics = registry.match_aliases(normalized)
    channel_intent = sales_channel_intent(query)
    if channel_intent.explicit and not any(
        marker in normalized for marker in ("стоп", "нагруз", "выдач")
    ):
        if "средн" in normalized and "чек" in normalized:
            metrics = ["average_check"]
        elif "заказ" in normalized and not any(
            marker in normalized for marker in ("продаж", "выруч", "оборот")
        ):
            metrics = ["orders_count"]
        elif any(marker in normalized for marker in ("продаж", "выруч", "оборот")):
            metrics = ["sales_by_channel" if channel_intent.split else "sales"]
    if not metrics:
        return PlannerResult(
            plan=ReportPlan(
                status=PlanStatus.UNSUPPORTED,
                unsupported_reason="Запрошенная метрика отсутствует в реестре MVP",
            )
        )
    date_from, date_to = _period(query, today)
    if date_from is None:
        return PlannerResult(
            plan=ReportPlan(
                status=PlanStatus.NEEDS_CLARIFICATION,
                clarification_question="За какой период нужен отчёт?",
                metric_ids=metrics,
            )
        )
    units = UUID_RE.findall(query) or default_units
    wants_all = "все мои заведени" in normalized or "по заведениям" in normalized
    if not units and not wants_all:
        return PlannerResult(
            plan=ReportPlan(
                status=PlanStatus.NEEDS_CLARIFICATION,
                clarification_question="По каким заведениям нужен отчёт?",
                metric_ids=metrics,
                date_from=date_from,
                date_to=date_to,
            )
        )
    granularity = Granularity.TOTAL
    if "по дням" in normalized:
        granularity = Granularity.DAY
    elif "по недел" in normalized:
        granularity = Granularity.WEEK
    elif "по месяц" in normalized:
        granularity = Granularity.MONTH
    output = (
        OutputFormat.XLSX
        if "xlsx" in normalized or "excel" in normalized
        else OutputFormat.CSV
        if "csv" in normalized
        else OutputFormat.TABLE
    )
    filters = (
        [ReportFilter(name="salesChannel", values=list(channel_intent.values))]
        if channel_intent.values
        else []
    )
    operations = list(dict.fromkeys(registry.get(item).operation_id for item in metrics))
    group_by = ([] if granularity == Granularity.TOTAL else [granularity.value]) + ["unit"]
    if channel_intent.split:
        group_by.append(SALES_CHANNEL_GROUP)
    return PlannerResult(
        plan=ReportPlan(
            status=PlanStatus.READY,
            metric_ids=metrics,
            operation_ids=operations,
            date_from=date_from,
            date_to=date_to,
            unit_references=units or ["all"],
            granularity=granularity,
            group_by=group_by,
            filters=filters,
            output_format=output,
            selected_response_fields=list(
                dict.fromkeys(
                    field for metric in metrics for field in registry.get(metric).required_fields
                )
            ),
            explanation="План построен локальным демонстрационным планировщиком.",
        )
    )
