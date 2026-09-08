from __future__ import annotations

from app.documentation.models import EndpointDocument
from app.documentation.repository import DocumentationRepository
from app.dodo.profile import ProfileResolver, derive_profile
from app.errors import ConfigurationError
from app.reports.metric_registry import MetricDefinition, MetricRegistry

NUMERIC_TYPES = {"integer", "number"}
GROUP_FIELDS = {
    "sales channel": {"salesChannel", "salesChannelName"},
    "ingredient": {"ingredientId", "ingredientName"},
    "ingredient category": {"ingredientCategoryId", "ingredientCategoryName"},
    "stop reason": {"reason"},
}


def validate_metric_contracts(
    registry: MetricRegistry,
    repository: DocumentationRepository,
    allowed_operations: set[str],
    profiles: ProfileResolver,
) -> None:
    """Fail fast when a registered metric disagrees with the bundled API contract."""
    problems: list[str] = []
    for metric_id in sorted(registry.all()):
        definition = registry.get(metric_id)
        endpoint = repository.get(definition.operation_id)
        if endpoint is None:
            problems.append(f"{metric_id}: операция {definition.operation_id} не найдена")
            continue
        if endpoint.method != "GET" or endpoint.deprecated:
            problems.append(f"{metric_id}: операция должна быть актуальным GET endpoint")
        if definition.operation_id not in allowed_operations:
            problems.append(f"{metric_id}: операция отсутствует в allowlist")
        profile = profiles.resolve(endpoint)
        documented_profile = derive_profile(
            endpoint,
            default_max_period_days=profiles.default_max_period_days,
            page_size=profiles.page_size,
        )
        if documented_profile.pagination and not profile.pagination:
            problems.append(f"{metric_id}: пагинация из документации отключена конфигурацией")
        if not profile.has_units or not profile.has_period:
            problems.append(f"{metric_id}: профиль не содержит units или периода")
        if profile.collection != definition.collection:
            problems.append(
                f"{metric_id}: collection={definition.collection!r}, "
                f"профиль использует {profile.collection!r}"
            )
        fields = _relative_fields(endpoint, definition.collection)
        for field in definition.required_fields:
            if field not in fields:
                problems.append(f"{metric_id}: поле {field!r} отсутствует в документации")
        if definition.nested_collection and not any(
            field == definition.nested_collection
            or field.startswith(f"{definition.nested_collection}.")
            for field in fields
        ):
            problems.append(
                f"{metric_id}: вложенная коллекция {definition.nested_collection!r} "
                "отсутствует в документации"
            )
        for group in definition.groups:
            expected_fields = GROUP_FIELDS.get(group)
            if expected_fields and not any(
                field.split(".")[-1] in expected_fields for field in fields
            ):
                problems.append(f"{metric_id}: группировка {group!r} не имеет поля в ответе")
        if definition.deduplication_field and not _field_types(
            definition.deduplication_field, definition, fields
        ):
            problems.append(
                f"{metric_id}: поле дедупликации "
                f"{definition.deduplication_field!r} отсутствует в документации"
            )
        _check_numeric_formula_fields(metric_id, definition, fields, problems)
        if profile.pagination:
            if profile.items_path != definition.collection:
                problems.append(f"{metric_id}: пагинатор читает не collection метрики")
            if not profile.take_parameter or not profile.end_flag_path:
                problems.append(f"{metric_id}: профиль пагинации неполон")
            parameter_types = {item.name: item.type for item in endpoint.parameters}
            if parameter_types.get(profile.skip_parameter) != "integer":
                problems.append(f"{metric_id}: skip-параметр пагинации не является integer")
            if parameter_types.get(profile.take_parameter or "") != "integer":
                problems.append(f"{metric_id}: take-параметр пагинации не является integer")
            end_flag_types = {
                field.type
                for field in endpoint.response_fields
                if field.path.replace("[]", "").strip(".") == profile.end_flag_path
            }
            if end_flag_types != {"boolean"}:
                problems.append(f"{metric_id}: end-флаг пагинации не является boolean")
            if profile.page_size <= 0 or profile.page_size > 1000:
                problems.append(f"{metric_id}: размер страницы пагинации вне диапазона 1..1000")
    if problems:
        raise ConfigurationError(
            "Некорректные контракты метрик: " + "; ".join(problems),
            details={"problems": problems},
        )


def _relative_fields(endpoint: EndpointDocument, collection: str) -> dict[str, str]:
    result: dict[str, str] = {}
    prefix = collection.replace("[]", "").strip(".")
    for field in endpoint.response_fields:
        path = field.path.replace("[]", "").strip(".")
        if prefix:
            if path == prefix:
                relative = ""
            elif path.startswith(f"{prefix}."):
                relative = path[len(prefix) + 1 :]
            else:
                continue
        else:
            relative = path
        if relative:
            result[relative] = field.type
    return result


def _check_numeric_formula_fields(
    metric_id: str,
    definition: MetricDefinition,
    fields: dict[str, str],
    problems: list[str],
) -> None:
    if definition.aggregation in {"sum", "average", "min", "max"}:
        pointers = (definition.value_field,)
    elif definition.aggregation == "weighted_average":
        pointers = (definition.value_field, definition.weight_field)
    elif definition.aggregation == "ratio_from_sums":
        pointers = (definition.numerator_field, definition.denominator_field)
    else:
        pointers = ()
    for pointer in (item for item in pointers if item):
        matching_types = _field_types(pointer, definition, fields)
        if not matching_types:
            problems.append(f"{metric_id}: поле формулы {pointer!r} отсутствует в документации")
        elif not matching_types <= NUMERIC_TYPES:
            problems.append(f"{metric_id}: поле формулы {pointer!r} не является числовым")


def _field_types(
    pointer: str,
    definition: MetricDefinition,
    fields: dict[str, str],
) -> set[str]:
    if definition.nested_collection:
        nested_path = f"{definition.nested_collection}.{pointer}"
        if nested_path in fields:
            return {fields[nested_path]}
    if pointer in fields:
        return {fields[pointer]}
    return {field_type for field, field_type in fields.items() if field.endswith(f".{pointer}")}
