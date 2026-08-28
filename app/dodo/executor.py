from __future__ import annotations

from contextlib import AbstractContextManager
from datetime import date, datetime, time
from typing import Any, Protocol

from app.documentation.models import EndpointDocument
from app.documentation.repository import DocumentationRepository
from app.dodo.chunker import chunk_dates, chunk_units, report_buckets
from app.dodo.paginator import paginate
from app.dodo.profile import ROOT_COLLECTION, ProfileResolver
from app.errors import ConfigurationError, DodoApiError, PlanValidationError
from app.planner.schemas import Granularity, PlanMode, ReportPlan
from app.reports.metric_registry import MetricRegistry


class OperationClient(Protocol):
    request_count: int

    def request_scope(self) -> AbstractContextManager[list[int]]: ...

    async def request_operation(
        self,
        operation: Any,
        query_params: dict[str, Any],
        path_values: dict[str, str] | None = ...,
    ) -> dict: ...


def format_date_bounds(mode: str, start: date, end: date) -> tuple[str, str]:
    if mode == "date":
        return start.isoformat(), end.isoformat()
    if mode == "datetime_hour":
        return (
            datetime.combine(start, time.min).isoformat(),
            datetime.combine(end, time.max).replace(minute=0, second=0).isoformat(),
        )
    return (
        datetime.combine(start, time.min).isoformat(),
        datetime.combine(end, time.max).isoformat(),
    )


def _extract_records(payload: dict[str, Any], collection: str) -> list[dict[str, Any]]:
    if collection == ROOT_COLLECTION:
        value: Any = payload.get(ROOT_COLLECTION, [])
    else:
        value = payload
        for part in collection.split(".") if collection else []:
            value = value.get(part) if isinstance(value, dict) else None
    if isinstance(value, list):
        return [dict(row) for row in value if isinstance(row, dict)]
    if isinstance(value, dict):
        return [dict(value)]
    return []


NATIVE_GRANULARITY = {
    "get-finances-sales-daily-units": {Granularity.DAY, Granularity.WEEK, Granularity.MONTH},
    "get-production-unit-workload-by-orders": {
        Granularity.DAY,
        Granularity.WEEK,
        Granularity.MONTH,
    },
    "get-production-unit-workload-by-products": {
        Granularity.DAY,
        Granularity.WEEK,
        Granularity.MONTH,
    },
}


class DodoExecutor:
    def __init__(
        self,
        client: OperationClient,
        repository: DocumentationRepository,
        registry: MetricRegistry,
        overrides: dict[str, dict[str, Any]],
        allowed_operations: set[str],
        *,
        allow_all_get: bool = False,
        settings_values: dict[str, str] | None = None,
        default_max_period_days: int = 31,
        raw_max_rows: int = 100_000,
    ) -> None:
        self.client = client
        self.repository = repository
        self.registry = registry
        self.overrides = overrides
        self.allowed_operations = allowed_operations
        self.allow_all_get = allow_all_get
        self.settings_values = settings_values or {}
        self.raw_max_rows = raw_max_rows
        self.profiles = ProfileResolver(overrides, default_max_period_days=default_max_period_days)

    def _operation(self, operation_id: str) -> EndpointDocument:
        if not self.allow_all_get and operation_id not in self.allowed_operations:
            raise PlanValidationError("Executor отклонил операцию вне allowlist")
        operation = self.repository.get(operation_id)
        if operation is None:
            raise PlanValidationError("Операция отсутствует в локальном индексе")
        if operation.method != "GET" or operation.deprecated:
            raise PlanValidationError("Разрешено выполнение только актуальных GET-операций")
        return operation

    async def execute(
        self, plan: ReportPlan, unit_ids: list[str]
    ) -> dict[str, list[dict[str, Any]]]:
        if plan.mode in {PlanMode.DYNAMIC, PlanMode.RAW}:
            return await self._execute_dynamic(plan, unit_ids)
        return await self._execute_metrics(plan, unit_ids)

    async def _execute_metrics(
        self, plan: ReportPlan, unit_ids: list[str]
    ) -> dict[str, list[dict[str, Any]]]:
        result: dict[str, list[dict[str, Any]]] = {item: [] for item in plan.operation_ids}
        assert plan.date_from and plan.date_to
        for operation_id in plan.operation_ids:
            operation = self._operation(operation_id)
            override = self.profiles.resolve(operation).as_override()
            unit_groups = chunk_units(unit_ids, int(override.get("max_units", 30)))
            if operation_id == "get-orders-client-statistics" and "unit" in plan.group_by:
                unit_groups = [[unit_id] for unit_id in unit_ids]
            buckets = (
                report_buckets(plan.date_from, plan.date_to, plan.granularity)
                if plan.granularity != Granularity.TOTAL
                and plan.granularity not in NATIVE_GRANULARITY.get(operation_id, set())
                else [type("Range", (), {"start": plan.date_from, "end": plan.date_to})()]
            )
            for bucket in buckets:
                ranges = chunk_dates(
                    bucket.start, bucket.end, int(override.get("max_period_days", 31))
                )
                for date_range in ranges:
                    for units in unit_groups:
                        params = self._params(override, units, date_range.start, date_range.end)
                        if override.get("pagination"):
                            dedup = next(
                                (
                                    self.registry.get(metric_id).deduplication_field
                                    for metric_id in plan.metric_ids
                                    if self.registry.get(metric_id).operation_id == operation_id
                                    and self.registry.get(metric_id).deduplication_field
                                ),
                                None,
                            )

                            async def fetch(
                                page: dict[str, int],
                                operation_document: Any = operation,
                                base_params: dict[str, Any] = params,
                            ) -> dict[str, Any]:
                                return await self.client.request_operation(
                                    operation_document, {**base_params, **page}
                                )

                            items = await paginate(fetch, override, deduplication_field=dedup)
                            payload: dict[str, Any] = {
                                str(override["items_path"]): items,
                                str(override.get("end_flag_path", "isEndOfListReached")): True,
                            }
                        else:
                            payload = await self.client.request_operation(operation, params)
                        records = self._records(operation, payload, plan)
                        for record in records:
                            record["__bucket_start"] = bucket.start.isoformat()
                            record["__bucket_end"] = bucket.end.isoformat()
                            record["__period_start"] = plan.date_from.isoformat()
                            record["__period_end"] = plan.date_to.isoformat()
                            if "unitId" not in record and len(units) == 1:
                                record["unitId"] = units[0]
                        result[operation_id].extend(records)
        return result

    async def _execute_dynamic(
        self, plan: ReportPlan, unit_ids: list[str]
    ) -> dict[str, list[dict[str, Any]]]:
        """Execute an operation using dynamic aggregation from the plan."""
        result: dict[str, list[dict[str, Any]]] = {}
        agg = plan.dynamic_aggregation
        for operation_id in plan.operation_ids:
            operation = self._operation(operation_id)
            profile = self.profiles.resolve(operation)
            override = profile.as_override()
            collection = (
                (agg.collection if agg else "") or plan.raw_collection or profile.collection
            )
            if collection == ROOT_COLLECTION:
                collection = ROOT_COLLECTION
            query_arguments = {
                item.name: item.value
                for item in plan.operation_arguments
                if item.location == "query"
            }
            path_arguments = {
                item.name: item.value
                for item in plan.operation_arguments
                if item.location == "path"
            }
            parameter_locations = {
                item.name: item.location or "query" for item in operation.parameters
            }
            for parameter_name, setting_name in profile.settings_parameters.items():
                value = self.settings_values.get(setting_name, "")
                parameter = next(
                    (item for item in operation.parameters if item.name == parameter_name),
                    None,
                )
                if not value and parameter and parameter.required:
                    raise ConfigurationError(
                        f"Для endpoint «{operation_id}» не настроен {setting_name.upper()}"
                    )
                if not value:
                    continue
                if parameter_locations.get(parameter_name) == "path":
                    path_arguments[parameter_name] = value
                else:
                    query_arguments[parameter_name] = value
            unit_groups = (
                chunk_units(unit_ids, profile.units_per_request)
                if profile.has_units and unit_ids
                else [[]]
            )
            ranges = (
                chunk_dates(plan.date_from, plan.date_to, profile.max_period_days)
                if profile.has_period and plan.date_from and plan.date_to
                else [None]
            )
            rows: list[dict[str, Any]] = []
            for units in unit_groups:
                for date_range in ranges:
                    params: dict[str, Any] = dict(query_arguments)
                    if units and profile.units_parameter:
                        params[profile.units_parameter] = units
                    if date_range is not None:
                        start, end = format_date_bounds(
                            profile.date_mode, date_range.start, date_range.end
                        )
                        params[str(profile.from_parameter)] = start
                        params[str(profile.to_parameter)] = end
                    if override.get("pagination"):

                        async def fetch(
                            page: dict[str, int],
                            op: Any = operation,
                            base: dict[str, Any] = params,
                            path: dict[str, str] = path_arguments,
                        ) -> dict[str, Any]:
                            return await self.client.request_operation(op, {**base, **page}, path)

                        items = await paginate(fetch, override)
                        payload: dict[str, Any] = {
                            str(override.get("items_path", collection)): items,
                            str(override.get("end_flag_path", "isEndOfListReached")): True,
                        }
                    else:
                        payload = await self.client.request_operation(
                            operation, params, path_arguments
                        )
                    for record in _extract_records(payload, collection):
                        if plan.date_from and plan.date_to:
                            record.setdefault("__period_start", plan.date_from.isoformat())
                            record.setdefault("__period_end", plan.date_to.isoformat())
                            record.setdefault("__bucket_start", plan.date_from.isoformat())
                            record.setdefault("__bucket_end", plan.date_to.isoformat())
                        if "unitId" not in record and len(units) == 1:
                            record["unitId"] = units[0]
                        rows.append(record)
                        if len(rows) > self.raw_max_rows:
                            raise DodoApiError(
                                f"Endpoint вернул больше {self.raw_max_rows} строк. "
                                "Сократите период или уточните параметры."
                            )
            result[operation_id] = rows
        return result

    @staticmethod
    def _params(
        override: dict[str, Any], units: list[str], start: date, end: date
    ) -> dict[str, Any]:
        from_value, to_value = format_date_bounds(str(override.get("date_mode")), start, end)
        return {
            "units": units,
            str(override["from_parameter"]): from_value,
            str(override["to_parameter"]): to_value,
        }

    def _records(
        self,
        operation: EndpointDocument,
        payload: dict[str, Any],
        plan: ReportPlan,
    ) -> list[dict[str, Any]]:
        definitions = [
            self.registry.get(item)
            for item in plan.metric_ids
            if self.registry.get(item).operation_id == operation.operation_id
        ]
        collection = definitions[0].collection if definitions else ""
        value: Any = payload
        for part in collection.split(".") if collection else []:
            value = value.get(part, []) if isinstance(value, dict) else []
        rows = value if isinstance(value, list) else [value] if isinstance(value, dict) else []
        nested = definitions[0].nested_collection if definitions else None
        if plan.filters:
            filter_names = {item.name for item in plan.filters}
            nested_candidates = {
                parts[-2]
                for field in operation.response_fields
                if field.path.replace("[]", "").split(".")[-1] in filter_names
                and len(parts := field.path.replace("[]", "").split(".")) >= 2
                and parts[-2] != collection
            }
            if len(nested_candidates) > 1:
                raise PlanValidationError("Фильтры относятся к разным вложенным коллекциям")
            nested = next(iter(nested_candidates), nested)
        if nested:
            expanded = []
            for row in rows:
                for child in row.get(nested, []):
                    expanded.append({**row, **child})
            return expanded
        return [dict(row) for row in rows if isinstance(row, dict)]
