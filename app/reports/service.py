from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime, time, timedelta
from typing import Any

from app.planner.schemas import (
    AggregationType,
    DynamicAggregation,
    Granularity,
    PlanMode,
    ReportPlan,
)
from app.reports.aggregations import (
    average_values,
    count_unique,
    ratio_from_sums,
    sum_values,
    weighted_average,
)
from app.reports.intervals import stop_duration_hours
from app.reports.metric_registry import MetricDefinition, MetricRegistry
from app.reports.presenters import deterministic_summary, present_value


def _dynamic_to_definition(agg: DynamicAggregation, operation_id: str) -> MetricDefinition:
    """Convert a DynamicAggregation to a MetricDefinition for unified processing."""
    aggregation_map = {
        AggregationType.SUM: "sum",
        AggregationType.COUNT: "count",
        AggregationType.COUNT_UNIQUE: "count_unique",
        AggregationType.AVERAGE: "average",
        AggregationType.WEIGHTED_AVERAGE: "weighted_average",
        AggregationType.RATIO: "ratio_from_sums",
        AggregationType.MIN: "min",
        AggregationType.MAX: "max",
    }
    return MetricDefinition(
        operation_id=operation_id,
        collection=agg.collection,
        aggregation=aggregation_map.get(agg.aggregation, "sum"),
        value_field=agg.value_field,
        weight_field=agg.weight_field,
        numerator_field=agg.numerator_field,
        denominator_field=agg.denominator_field,
        required_fields=[agg.value_field],
        granularities=["total", "hour", "day", "week", "month"],
        groups=["unit", "hour", "day", "week", "month"],
    )


class ReportResult:
    def __init__(
        self,
        columns: list[str],
        rows: list[dict[str, Any]],
        totals: dict[str, Any],
        summary: str | None = None,
    ):
        self.columns = columns
        self.rows = rows
        self.totals = totals
        self.summary = summary or deterministic_summary(rows, [key for key in totals])


class ReportAggregator:
    def __init__(self, registry: MetricRegistry, unit_names: dict[str, str] | None = None) -> None:
        self.registry = registry
        self.unit_names = unit_names or {}

    def aggregate(
        self, plan: ReportPlan, records_by_operation: dict[str, list[dict[str, Any]]]
    ) -> ReportResult:
        records_by_operation = {
            operation_id: [row for row in rows if self._matches_filters(plan, row)]
            for operation_id, rows in records_by_operation.items()
        }
        if plan.mode == PlanMode.RAW:
            return self._raw_result(plan, records_by_operation)
        if plan.mode == PlanMode.DYNAMIC and plan.dynamic_aggregation:
            return self._aggregate_dynamic(plan, records_by_operation)
        return self._aggregate_metrics(plan, records_by_operation)

    @staticmethod
    def _matches_filters(plan: ReportPlan, row: dict[str, Any]) -> bool:
        field_aliases = {
            "salesChannel": ("salesChannel", "salesChannelName"),
            "orderSource": ("orderSource", "orderSourceName"),
            "paymentMethod": ("paymentMethod", "paymentMethodName"),
        }
        for report_filter in plan.filters:
            accepted = {str(value).casefold() for value in report_filter.values}
            if not accepted:
                continue
            actual = next(
                (
                    row.get(field)
                    for field in field_aliases[report_filter.name]
                    if row.get(field) is not None
                ),
                None,
            )
            if actual is None or str(actual).casefold() not in accepted:
                return False
        return True

    def _raw_result(
        self, plan: ReportPlan, records_by_operation: dict[str, list[dict[str, Any]]]
    ) -> ReportResult:
        records = [
            row
            for operation_id in plan.operation_ids
            for row in records_by_operation.get(operation_id, [])
        ]
        rows = [self._flatten_raw_row(row) for row in records]
        if plan.selected_response_fields:
            selected = {item.replace("[]", "") for item in plan.selected_response_fields}
            rows = [
                {
                    key: value
                    for key, value in row.items()
                    if key in selected
                    or key.split(".")[-1] in selected
                    or any(item.endswith(key) for item in selected)
                }
                for row in rows
            ]
        columns = list(dict.fromkeys(key for row in rows for key in row))
        return ReportResult(
            columns,
            rows,
            {"rows_count": len(rows)},
            summary=f"Получено записей: {len(rows)}.",
        )

    @classmethod
    def _flatten_raw_row(cls, row: dict[str, Any], prefix: str = "") -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in row.items():
            if key.startswith("__"):
                continue
            name = f"{prefix}.{key}" if prefix else key
            if isinstance(value, dict):
                result.update(cls._flatten_raw_row(value, name))
            elif isinstance(value, list):
                result[name] = json.dumps(value, ensure_ascii=False)
            else:
                result[name] = value
        return result

    def _aggregate_metrics(
        self, plan: ReportPlan, records_by_operation: dict[str, list[dict[str, Any]]]
    ) -> ReportResult:
        groups: dict[tuple[Any, ...], list[dict[str, Any]]] = defaultdict(list)
        all_records = [
            row
            for operation_id in plan.operation_ids
            for row in records_by_operation.get(operation_id, [])
        ]
        for row in all_records:
            groups[self._group_key(plan, row)].append(row)
        output = []
        for key, rows in sorted(groups.items(), key=lambda item: str(item[0])):
            result = self._dimensions(plan, rows[0], key)
            for metric_id in plan.metric_ids:
                definition = self.registry.get(metric_id)
                metric_rows = records_by_operation.get(definition.operation_id, [])
                matching = [item for item in metric_rows if self._group_key(plan, item) == key]
                result[metric_id] = present_value(metric_id, self._calculate(definition, matching))
            output.append(result)
        totals = {
            metric_id: present_value(
                metric_id,
                self._calculate(
                    self.registry.get(metric_id),
                    records_by_operation.get(self.registry.get(metric_id).operation_id, []),
                ),
            )
            for metric_id in plan.metric_ids
        }
        columns = list(output[0]) if output else [*self._dimension_names(plan), *plan.metric_ids]
        return ReportResult(columns, output, totals)

    def _aggregate_dynamic(
        self, plan: ReportPlan, records_by_operation: dict[str, list[dict[str, Any]]]
    ) -> ReportResult:
        agg = plan.dynamic_aggregation
        if not agg or not plan.operation_ids:
            return ReportResult([], [], {})
        operation_id = plan.operation_ids[0]
        definition = _dynamic_to_definition(agg, operation_id)
        metric_label = agg.value_field
        groups: dict[tuple[Any, ...], list[dict[str, Any]]] = defaultdict(list)
        all_records = records_by_operation.get(operation_id, [])
        for row in all_records:
            groups[self._group_key(plan, row)].append(row)
        output = []
        for key, rows in sorted(groups.items(), key=lambda item: str(item[0])):
            result = self._dimensions(plan, rows[0], key) if rows else {}
            result[metric_label] = present_value(metric_label, self._calculate(definition, rows))
            output.append(result)
        total_value = self._calculate(definition, all_records)
        totals = {metric_label: present_value(metric_label, total_value)}
        columns = list(output[0]) if output else [*self._dimension_names(plan), metric_label]
        summary = (
            f"Агрегация {agg.aggregation.value}({agg.value_field}): {len(all_records)} записей"
        )
        return ReportResult(columns, output, totals, summary=summary)

    def _group_key(self, plan: ReportPlan, row: dict[str, Any]) -> tuple[Any, ...]:
        result: list[Any] = []
        if plan.granularity != Granularity.TOTAL:
            result.append(self._time_bucket(plan.granularity, row))
        if "unit" in plan.group_by:
            result.append(row.get("unitId", ""))
        if "sales channel" in plan.group_by:
            result.append(row.get("salesChannel") or row.get("salesChannelName"))
        if "ingredient" in plan.group_by:
            result.append(row.get("ingredientName") or row.get("ingredientId"))
        if "ingredient category" in plan.group_by:
            result.append(row.get("ingredientCategoryName") or row.get("ingredientCategoryId"))
        if "stop reason" in plan.group_by:
            result.append(row.get("reason"))
        return tuple(result) or ("total",)

    @staticmethod
    def _time_bucket(granularity: Granularity, row: dict[str, Any]) -> Any:
        raw = next(
            (
                row.get(field)
                for field in (
                    "date",
                    "fromLocal",
                    "startedAtLocal",
                    "createdAtLocal",
                    "createdAt",
                    "__bucket_start",
                )
                if row.get(field)
            ),
            None,
        )
        if raw is None:
            return None
        value = str(raw)
        if granularity == Granularity.HOUR:
            return f"{value[:13]}:00:00"
        if granularity == Granularity.DAY:
            return value[:10]
        try:
            current = datetime.fromisoformat(value.replace("Z", "+00:00")).date()
        except ValueError:
            return value
        if granularity == Granularity.WEEK:
            return (current - timedelta(days=current.weekday())).isoformat()
        if granularity == Granularity.MONTH:
            return current.replace(day=1).isoformat()
        return value

    def _dimensions(
        self, plan: ReportPlan, row: dict[str, Any], key: tuple[Any, ...]
    ) -> dict[str, Any]:
        result = {}
        index = 0
        if plan.granularity != Granularity.TOTAL:
            result[plan.granularity.value] = key[index]
            index += 1
        if "unit" in plan.group_by:
            unit_id = key[index]
            result["unitId"] = unit_id
            result["unitName"] = self.unit_names.get(str(unit_id), "")
            index += 1
        for group, field in (
            ("sales channel", "salesChannel"),
            ("ingredient", "ingredient"),
            ("ingredient category", "ingredientCategory"),
            ("stop reason", "stopReason"),
        ):
            if group in plan.group_by:
                result[field] = key[index]
                index += 1
        return result

    @staticmethod
    def _dimension_names(plan: ReportPlan) -> list[str]:
        names = [] if plan.granularity == Granularity.TOTAL else [plan.granularity.value]
        if "unit" in plan.group_by:
            names += ["unitId", "unitName"]
        for group, field in (
            ("sales channel", "salesChannel"),
            ("ingredient", "ingredient"),
            ("ingredient category", "ingredientCategory"),
            ("stop reason", "stopReason"),
        ):
            if group in plan.group_by:
                names.append(field)
        return names

    @staticmethod
    def _calculate(definition: MetricDefinition, rows: list[dict[str, Any]]) -> Any:
        if definition.aggregation == "sum":
            return sum_values(rows, definition.value_field or "")
        if definition.aggregation == "count_unique":
            return count_unique(rows, definition.value_field or "")
        if definition.aggregation == "weighted_average":
            return weighted_average(
                rows, definition.value_field or "", definition.weight_field or ""
            )
        if definition.aggregation == "average":
            return average_values(rows, definition.value_field or "")
        if definition.aggregation == "ratio_from_sums":
            return ratio_from_sums(
                rows,
                definition.numerator_field or "",
                definition.denominator_field or "",
                definition.multiplier,
            )
        if definition.aggregation == "duration_sum":
            if not rows:
                return 0.0
            start = datetime.combine(
                min(datetime.fromisoformat(row["__bucket_start"]).date() for row in rows),
                time.min,
            )
            end = datetime.combine(
                max(datetime.fromisoformat(row["__bucket_end"]).date() for row in rows),
                time.max,
            )
            return stop_duration_hours(rows, start, end)
        if definition.aggregation == "count":
            return len(rows)
        if definition.aggregation == "min":
            values = [row.get(definition.value_field or "") for row in rows]
            return min((item for item in values if item is not None), default=None)
        if definition.aggregation == "max":
            values = [row.get(definition.value_field or "") for row in rows]
            return max((item for item in values if item is not None), default=None)
        return None
