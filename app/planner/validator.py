from __future__ import annotations

import re
from datetime import date, timedelta

from app.documentation.models import EndpointCandidate, EndpointDocument
from app.documentation.repository import DocumentationRepository
from app.dodo.profile import ProfileResolver
from app.errors import PlanValidationError
from app.planner.schemas import (
    AggregationType,
    DynamicAggregation,
    PlanMode,
    PlanStatus,
    ReportPlan,
)
from app.reports.metric_registry import MetricRegistry
from app.retrieval.normalizer import normalize_query

UNIT_RE = re.compile(r"^(?:[0-9a-fA-F]{32}|[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12})$")
ALLOWED_FILTERS = {"salesChannel", "orderSource", "paymentMethod"}
VALID_AGGREGATIONS = {item.value for item in AggregationType}


class ReportPlanValidator:
    def __init__(
        self,
        registry: MetricRegistry,
        repository: DocumentationRepository,
        allowed_operations: set[str],
        *,
        allow_all_get: bool = False,
        profiles: ProfileResolver | None = None,
    ) -> None:
        self.registry = registry
        self.repository = repository
        self.allowed_operations = allowed_operations
        self.allow_all_get = allow_all_get
        self.profiles = profiles or ProfileResolver({})

    def apply_query_intent(self, plan: ReportPlan, query: str) -> ReportPlan:
        if plan.status != PlanStatus.READY or plan.mode != PlanMode.RAW:
            return plan
        if len(plan.operation_ids) != 1:
            return plan
        normalized = normalize_query(query)
        average_markers = ("средн", "average", "среднее значение")
        sum_markers = (
            "сумм",
            "итого",
            "сколько денег",
            "количество денег",
            "потрачен",
            "общая стоимость",
            "общая цена",
        )
        if any(marker in normalized for marker in average_markers):
            aggregation = AggregationType.AVERAGE
        elif any(marker in normalized for marker in sum_markers):
            aggregation = AggregationType.SUM
        else:
            return plan

        endpoint = self.repository.get(plan.operation_ids[0])
        if endpoint is None:
            return plan
        profile = self.profiles.resolve(endpoint)
        collection = plan.raw_collection or profile.collection
        numeric_fields = []
        for field in endpoint.response_fields:
            if field.type not in {"integer", "number"}:
                continue
            path = field.path.replace("[]", "").strip(".")
            if collection and collection != "__root__":
                prefix = f"{collection}."
                if not path.startswith(prefix):
                    continue
                path = path[len(prefix) :]
            if "." in path:
                continue
            numeric_fields.append((path, field))
        if not numeric_fields:
            return plan

        preferred_names: tuple[str, ...] = ()
        if any(word in normalized for word in ("деньг", "стоим", "цен", "рубл")):
            preferred_names = ("price", "cost", "amount", "sales", "revenue")
        elif any(word in normalized for word in ("количеств", "объем", "объём")):
            preferred_names = ("quantity", "count", "amount", "volume")
        elif any(word in normalized for word in ("врем", "длительност")):
            preferred_names = ("time", "duration", "seconds", "minutes", "hours")

        preferred = [
            item
            for item in numeric_fields
            if any(marker in item[0].casefold() for marker in preferred_names)
        ]
        if len(preferred) == 1:
            selected = preferred[0]
        elif len(numeric_fields) == 1:
            selected = numeric_fields[0]
        else:
            query_tokens = {token for token in normalized.split() if len(token) >= 4}

            def score(item: tuple[str, object]) -> int:
                field = item[1]
                text = normalize_query(f"{item[0]} {getattr(field, 'description', '')}")
                return sum(token in text for token in query_tokens)

            ranked = sorted(
                ((score(item), item) for item in numeric_fields),
                key=lambda item: (-item[0], item[1][0]),
            )
            if not ranked or ranked[0][0] <= 0:
                return plan
            if len(ranked) > 1 and ranked[0][0] == ranked[1][0]:
                return plan
            selected = ranked[0][1]

        plan.mode = PlanMode.DYNAMIC
        plan.dynamic_aggregation = DynamicAggregation(
            value_field=selected[0],
            aggregation=aggregation,
            collection=collection,
        )
        plan.raw_collection = ""
        plan.selected_response_fields = []
        return plan

    def validate(
        self,
        plan: ReportPlan,
        candidates: list[EndpointCandidate],
        resolved_unit_ids: list[str] | None = None,
    ) -> ReportPlan:
        if plan.status != PlanStatus.READY:
            return plan
        if plan.mode == PlanMode.DYNAMIC:
            return self._validate_dynamic(plan, candidates, resolved_unit_ids)
        if plan.mode == PlanMode.RAW:
            return self._validate_raw(plan, candidates, resolved_unit_ids)
        return self._validate_metrics(plan, candidates, resolved_unit_ids)

    def _validate_metrics(
        self,
        plan: ReportPlan,
        candidates: list[EndpointCandidate],
        resolved_unit_ids: list[str] | None = None,
    ) -> ReportPlan:
        if plan.dynamic_aggregation is not None or plan.raw_collection:
            raise PlanValidationError("Metrics-режим содержит поля другого режима")
        if not plan.date_from or not plan.date_to:
            plan.status = PlanStatus.NEEDS_CLARIFICATION
            plan.clarification_question = "За какой период нужен отчёт?"
            return plan
        if plan.date_from > plan.date_to:
            raise PlanValidationError("Начало периода находится после конца")
        if plan.date_to > date.today() + timedelta(days=366 * 5):
            raise PlanValidationError("Период находится слишком далеко в будущем")
        if not plan.metric_ids or any(not self.registry.has(item) for item in plan.metric_ids):
            raise PlanValidationError("План содержит неизвестную метрику")
        candidate_ids = {item.operation_id for item in candidates}
        expected = {self.registry.get(item).operation_id for item in plan.metric_ids}
        if set(plan.operation_ids) != expected:
            raise PlanValidationError("Операции плана не соответствуют реестру метрик")
        endpoints = self._check_operations(plan, candidate_ids)
        self._check_response_fields(plan, endpoints)
        for metric_id in plan.metric_ids:
            definition = self.registry.get(metric_id)
            if plan.granularity.value not in definition.granularities:
                raise PlanValidationError("Метрика не поддерживает выбранную гранулярность")
        self._check_filters(plan, endpoints)
        units = resolved_unit_ids if resolved_unit_ids is not None else plan.unit_references
        if not units:
            raise PlanValidationError("Не выбраны заведения")
        self._check_unit_ids(units)
        return plan

    def _validate_dynamic(
        self,
        plan: ReportPlan,
        candidates: list[EndpointCandidate],
        resolved_unit_ids: list[str] | None = None,
    ) -> ReportPlan:
        if plan.metric_ids or plan.raw_collection:
            raise PlanValidationError("Dynamic-режим содержит поля другого режима")
        if not plan.operation_ids:
            raise PlanValidationError("Не удалось определить подходящий эндпоинт")
        if len(plan.operation_ids) > 1:
            raise PlanValidationError("Dynamic-режим поддерживает только один эндпоинт")
        if not plan.dynamic_aggregation:
            raise PlanValidationError("В dynamic_aggregation отсутствует способ агрегации")
        agg = plan.dynamic_aggregation
        if agg.aggregation.value not in VALID_AGGREGATIONS:
            raise PlanValidationError(f"Неподдерживаемый тип агрегации: {agg.aggregation}")
        if agg.aggregation == AggregationType.WEIGHTED_AVERAGE and not agg.weight_field:
            raise PlanValidationError("weighted_average требует weight_field")
        if agg.aggregation == AggregationType.RATIO and (
            not agg.numerator_field or not agg.denominator_field
        ):
            raise PlanValidationError("ratio требует numerator_field и denominator_field")
        if plan.date_from and plan.date_to:
            if plan.date_from > plan.date_to:
                raise PlanValidationError("Начало периода находится после конца")
            if plan.date_to > date.today() + timedelta(days=366 * 5):
                raise PlanValidationError("Период находится слишком далеко в будущем")
        candidate_ids = {item.operation_id for item in candidates}
        if plan.operation_ids[0] not in candidate_ids:
            raise PlanValidationError("Модель выбрала операцию вне списка кандидатов")
        endpoints = self._check_operations(plan, candidate_ids)
        self._check_filters(plan, endpoints)
        self._canonicalize_dynamic_aggregation(plan)
        for endpoint in endpoints:
            profile = self.profiles.resolve(endpoint)
            response_field_paths = {
                field.path.replace("[]", "").split(".")[-1] for field in endpoint.response_fields
            }
            if agg.value_field not in response_field_paths:
                all_paths = {field.path for field in endpoint.response_fields}
                if not any(agg.value_field in path for path in all_paths):
                    return self._fallback_dynamic_to_raw(
                        plan, candidates, resolved_unit_ids, profile
                    )
            if agg.weight_field and agg.weight_field not in response_field_paths:
                all_paths = {field.path for field in endpoint.response_fields}
                if not any(agg.weight_field in path for path in all_paths):
                    return self._fallback_dynamic_to_raw(
                        plan, candidates, resolved_unit_ids, profile
                    )
            if profile.has_period and not (plan.date_from and plan.date_to):
                plan.status = PlanStatus.NEEDS_CLARIFICATION
                plan.clarification_question = "За какой период нужен отчёт?"
                return plan
            if (
                agg.collection
                and profile.collection_candidates
                and agg.collection not in profile.collection_candidates
            ):
                raise PlanValidationError(
                    f"Коллекция '{agg.collection}' не найдена. "
                    f"Доступны: {', '.join(profile.collection_candidates)}"
                )
            self._check_operation_arguments(plan, endpoint, profile)
        units = resolved_unit_ids if resolved_unit_ids is not None else plan.unit_references
        if units:
            self._check_unit_ids(units)
        elif any(self.profiles.resolve(endpoint).has_units for endpoint in endpoints):
            raise PlanValidationError("Не выбраны заведения")
        return plan

    def _fallback_dynamic_to_raw(
        self,
        plan: ReportPlan,
        candidates: list[EndpointCandidate],
        resolved_unit_ids: list[str] | None,
        profile: object,
    ) -> ReportPlan:
        plan.mode = PlanMode.RAW
        plan.dynamic_aggregation = None
        plan.selected_response_fields = []
        collection_candidates = getattr(profile, "collection_candidates", [])
        plan.raw_collection = (
            collection_candidates[0]
            if collection_candidates
            else getattr(profile, "collection", "")
        )
        return self._validate_raw(plan, candidates, resolved_unit_ids)

    @staticmethod
    def _canonicalize_dynamic_aggregation(plan: ReportPlan) -> None:
        agg = plan.dynamic_aggregation
        if agg is None:
            return
        collection = agg.collection.replace("[]", "").strip(".")
        for attribute in (
            "value_field",
            "weight_field",
            "numerator_field",
            "denominator_field",
        ):
            raw = getattr(agg, attribute)
            if not raw:
                continue
            if not collection and "[]" in raw:
                collection = raw.split("[]", 1)[0].replace("[]", "").strip(".")
            normalized = raw.replace("[]", "").strip(".")
            prefix = f"{collection}." if collection else ""
            if prefix and normalized.startswith(prefix):
                normalized = normalized[len(prefix) :]
            elif "." in normalized:
                normalized = normalized.rsplit(".", 1)[-1]
            setattr(agg, attribute, normalized)
        agg.collection = collection

    def _validate_raw(
        self,
        plan: ReportPlan,
        candidates: list[EndpointCandidate],
        resolved_unit_ids: list[str] | None = None,
    ) -> ReportPlan:
        if plan.metric_ids:
            raise PlanValidationError("Raw-режим содержит metric_ids")
        if len(plan.operation_ids) != 1:
            plan.status = PlanStatus.UNSUPPORTED
            plan.unsupported_reason = "Не удалось однозначно выбрать один GET endpoint"
            return plan
        if plan.dynamic_aggregation is not None:
            raise PlanValidationError("Raw-режим не должен содержать dynamic_aggregation")
        if plan.date_from and plan.date_to and plan.date_from > plan.date_to:
            raise PlanValidationError("Начало периода находится после конца")
        if plan.date_to and plan.date_to > date.today() + timedelta(days=366 * 5):
            raise PlanValidationError("Период находится слишком далеко в будущем")
        candidate_ids = {item.operation_id for item in candidates}
        if plan.operation_ids[0] not in candidate_ids:
            plan.status = PlanStatus.UNSUPPORTED
            plan.unsupported_reason = "Выбранный endpoint отсутствует среди найденных кандидатов"
            return plan
        endpoints = self._check_operations(plan, candidate_ids)
        self._check_filters(plan, endpoints)
        endpoint = endpoints[0]
        profile = self.profiles.resolve(endpoint)
        if profile.has_period and not (plan.date_from and plan.date_to):
            plan.status = PlanStatus.NEEDS_CLARIFICATION
            plan.clarification_question = "За какой период нужен отчёт?"
            return plan
        if profile.collection_candidates:
            if not plan.raw_collection:
                plan.status = PlanStatus.NEEDS_CLARIFICATION
                plan.clarification_question = (
                    "Какие данные вывести: " + ", ".join(profile.collection_candidates) + "?"
                )
                return plan
            if plan.raw_collection not in profile.collection_candidates:
                plan.status = PlanStatus.NEEDS_CLARIFICATION
                plan.clarification_question = (
                    "Выберите раздел данных: " + ", ".join(profile.collection_candidates) + "."
                )
                return plan
        elif plan.raw_collection and plan.raw_collection not in {
            profile.collection,
            "__root__",
        }:
            raise PlanValidationError(
                f"Коллекция '{plan.raw_collection}' отсутствует в ответе операции"
            )
        self._check_response_fields(plan, endpoints)
        self._check_operation_arguments(plan, endpoint, profile)
        if plan.status != PlanStatus.READY:
            return plan
        units = resolved_unit_ids if resolved_unit_ids is not None else plan.unit_references
        if profile.has_units:
            if not units:
                raise PlanValidationError("Не выбраны заведения")
            self._check_unit_ids(units)
        elif units:
            self._check_unit_ids(units)
        return plan

    def _check_operations(
        self, plan: ReportPlan, candidate_ids: set[str]
    ) -> list[EndpointDocument]:
        endpoints = []
        for operation_id in plan.operation_ids:
            if operation_id not in candidate_ids:
                raise PlanValidationError("Модель выбрала операцию вне списка кандидатов")
            if not self.allow_all_get and operation_id not in self.allowed_operations:
                raise PlanValidationError("Операция не входит в allowlist")
            endpoint = self.repository.get(operation_id)
            if not endpoint or endpoint.method != "GET":
                raise PlanValidationError("Разрешено выполнение только известных GET-операций")
            if endpoint.deprecated:
                raise PlanValidationError("Операция помечена как устаревшая")
            endpoints.append(endpoint)
        return endpoints

    def _check_filters(self, plan: ReportPlan, endpoints: list[EndpointDocument]) -> None:
        if any(item.name not in ALLOWED_FILTERS or not item.values for item in plan.filters):
            raise PlanValidationError("План содержит неподдерживаемый или пустой фильтр")
        if plan.mode != PlanMode.METRICS and plan.filters:
            raise PlanValidationError("Фильтры поддерживаются только для зарегистрированных метрик")
        for report_filter in plan.filters:
            aliases = {report_filter.name, f"{report_filter.name}Name"}
            for endpoint in endpoints:
                matching_fields = [
                    field
                    for field in endpoint.response_fields
                    if field.path.replace("[]", "").split(".")[-1] in aliases
                ]
                if not matching_fields:
                    raise PlanValidationError(
                        f"Фильтр '{report_filter.name}' отсутствует в ответе "
                        f"операции '{endpoint.operation_id}'"
                    )
                documented_values = {
                    str(value).casefold() for field in matching_fields for value in field.enum
                }
                invalid_values = [
                    value
                    for value in report_filter.values
                    if documented_values and str(value).casefold() not in documented_values
                ]
                if invalid_values:
                    raise PlanValidationError(
                        f"Фильтр '{report_filter.name}' содержит неизвестные значения"
                    )

    def _check_operation_arguments(
        self,
        plan: ReportPlan,
        endpoint: EndpointDocument,
        profile: object,
    ) -> None:
        parameters = {item.name: item for item in endpoint.parameters}
        provided: dict[str, str] = {}
        normalized_arguments = []
        for argument in plan.operation_arguments:
            if argument.name in provided:
                continue
            parameter = parameters.get(argument.name)
            if parameter is None:
                continue
            expected_location = parameter.location or "query"
            argument.location = expected_location
            if parameter.enum and argument.value not in {str(item) for item in parameter.enum}:
                plan.status = PlanStatus.NEEDS_CLARIFICATION
                plan.clarification_question = (
                    f"Укажите значение параметра «{argument.name}»: "
                    + ", ".join(str(item) for item in parameter.enum)
                )
                return
            provided[argument.name] = argument.value
            normalized_arguments.append(argument)
        plan.operation_arguments = normalized_arguments

        auto_bound = {
            getattr(profile, "units_parameter", None),
            getattr(profile, "from_parameter", None),
            getattr(profile, "to_parameter", None),
            getattr(profile, "skip_parameter", None)
            if getattr(profile, "pagination", False)
            else None,
            getattr(profile, "take_parameter", None)
            if getattr(profile, "pagination", False)
            else None,
            *getattr(profile, "settings_parameters", {}).keys(),
        }
        missing = [
            item.name
            for item in endpoint.parameters
            if item.required and item.name not in provided and item.name not in auto_bound
        ]
        if missing:
            plan.status = PlanStatus.NEEDS_CLARIFICATION
            plan.clarification_question = (
                "Укажите обязательные параметры: " + ", ".join(missing) + "."
            )

    @staticmethod
    def _check_response_fields(plan: ReportPlan, endpoints: list[EndpointDocument]) -> None:
        known_fields = [
            field.path.replace("[]", "")
            for endpoint in endpoints
            for field in endpoint.response_fields
        ]
        valid = [
            field
            for field in plan.selected_response_fields
            if any(known.endswith(field.replace("[]", "")) for known in known_fields)
        ]
        if plan.mode == PlanMode.RAW:
            plan.selected_response_fields = valid
        elif len(valid) != len(plan.selected_response_fields):
            raise PlanValidationError("План содержит поле, отсутствующее в схеме ответа")

    @staticmethod
    def _check_unit_ids(units: list[str]) -> None:
        if units != ["all"] and any(not UNIT_RE.fullmatch(item) for item in units):
            raise PlanValidationError("Некорректный идентификатор заведения")
