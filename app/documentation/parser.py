from __future__ import annotations

import json
import re
from typing import Any

from app.documentation.models import (
    EndpointDocument,
    EndpointParameter,
    RawOperation,
    ResponseField,
)
from app.documentation.schema_flattener import flatten_schema


def _infer_aggregation_hint(field: ResponseField) -> str | None:
    """Infer aggregation type from field name and type patterns."""
    name_lower = field.path.lower()
    desc_lower = (field.description or "").lower()

    if field.type not in ("integer", "number", ""):
        return None

    sum_patterns = [
        r"count$",
        r"quantity$",
        r"amount$",
        r"total$",
        r"sales$",
        r"orders$",
        r"duration$",
        r"hours$",
        r"minutes$",
        r"seconds$",
        r"weight$",
        r"volume$",
        r"price$",
        r"cost$",
        r"revenue$",
    ]
    if any(re.search(pattern, name_lower) for pattern in sum_patterns):
        return "sum"

    avg_patterns = [r"^avg", r"average", r"mean"]
    if any(re.search(pattern, name_lower) for pattern in avg_patterns):
        return "weighted_average"

    percent_patterns = [r"percent", r"ratio", r"rate$", r"share$"]
    if any(re.search(pattern, name_lower) for pattern in percent_patterns):
        return "ratio"

    if "количество" in desc_lower or "число" in desc_lower or "сумма" in desc_lower:
        return "sum"
    if "среднее" in desc_lower or "средний" in desc_lower:
        return "weighted_average"
    if "процент" in desc_lower or "доля" in desc_lower:
        return "ratio"

    return None


def _build_aggregation_hints(response_fields: list[ResponseField]) -> str:
    """Build aggregation hints section for compact_summary."""
    hints: list[str] = []
    for field in response_fields[:30]:
        hint = _infer_aggregation_hint(field)
        if hint:
            hints.append(f"{field.path}→{hint}")
    if not hints:
        return ""
    return f"\nПодсказки агрегации: {', '.join(hints[:15])}"


def _detect_collection(schema: dict[str, Any] | None) -> str:
    """Detect the main data collection from response schema."""
    if not schema:
        return ""
    if schema.get("type") == "array":
        return "(корневой массив)"
    props = schema.get("properties", {})
    arrays = [
        name
        for name, prop in props.items()
        if isinstance(prop, dict) and prop.get("type") == "array"
    ]
    if len(arrays) == 1:
        return arrays[0]
    if arrays:
        return f"(несколько: {', '.join(arrays[:5])})"
    return ""


def _success_schema(responses: Any) -> dict[str, Any] | None:
    if isinstance(responses, list):
        pairs = [
            (str(item.get("status") or item.get("status_code") or ""), item) for item in responses
        ]
    elif isinstance(responses, dict):
        pairs = list(responses.items())
    else:
        return None
    pairs.sort(key=lambda pair: (pair[0] != "200", pair[0]))
    for status, response in pairs:
        if not status.startswith("2") or not isinstance(response, dict):
            continue
        candidates = [
            response.get("schema"),
            (response.get("content") or {}).get("application/json", {}).get("schema"),
            response.get("response_schema"),
        ]
        for schema in candidates:
            if isinstance(schema, dict):
                return schema
    return None


def parse_operation(operation: RawOperation) -> EndpointDocument:
    schema = _success_schema(operation.responses)
    response_fields = flatten_schema(schema)
    parameters = [
        EndpointParameter(
            name=str(item.get("name") or ""),
            location=str(item.get("in") or item.get("location") or ""),
            required=bool(item.get("required", False)),
            type=str((item.get("schema") or {}).get("type") or item.get("type") or ""),
            description=str(item.get("description") or ""),
            format=str((item.get("schema") or {}).get("format") or item.get("format") or ""),
            enum=list((item.get("schema") or {}).get("enum") or item.get("enum") or []),
        )
        for item in operation.parameters
        if item.get("name")
    ]
    field_text = " ".join(f"{field.path} {field.description}" for field in response_fields)
    parameter_text = " ".join(f"{item.name} {item.description}" for item in parameters)
    searchable = " ".join(
        [
            operation.id,
            operation.title,
            operation.path,
            operation.api,
            operation.description,
            parameter_text,
            field_text,
            " ".join(operation.scopes),
        ]
    )
    required = [parameter.name for parameter in parameters if parameter.required]
    collection = _detect_collection(schema)
    aggregation_hints = _build_aggregation_hints(response_fields)
    compact = (
        f"{operation.id}\n{operation.title}\n{operation.method.upper()} {operation.path}\n"
        f"{operation.description[:500]}\n"
        f"Обязательные параметры: {', '.join(required) or 'нет'}\n"
        f"Коллекция данных: {collection or 'объект'}\n"
        f"Поля ответа: {', '.join(field.path for field in response_fields[:25])}\n"
        f"Scopes: {', '.join(operation.scopes)}"
        f"{aggregation_hints}"
    )
    links = list(dict.fromkeys([*operation.links, *operation.all_links]))
    return EndpointDocument(
        operation_id=operation.id,
        api=operation.api,
        api_key=operation.api_key,
        title=operation.title,
        method=operation.method.upper(),
        path=operation.path,
        links=links,
        description=operation.description,
        scopes=operation.scopes,
        deprecated=operation.deprecated,
        parameters=parameters,
        response_fields=response_fields,
        success_response_schema=schema,
        searchable_text=searchable,
        compact_summary=compact,
    )


def serialize_endpoint(endpoint: EndpointDocument) -> str:
    return json.dumps(endpoint.model_dump(mode="json"), ensure_ascii=False)
