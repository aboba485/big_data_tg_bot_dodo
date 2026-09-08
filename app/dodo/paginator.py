from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from app.errors import DodoApiError

_MISSING = object()
logger = logging.getLogger(__name__)
MAX_SHAPE_FIELDS = 24
MAX_SHAPE_DEPTH = 3
SAFE_SHAPE_KEYS = {
    "__root__",
    "content",
    "data",
    "items",
    "records",
    "result",
    "results",
    "value",
    "values",
}


def get_path(payload: dict[str, Any], path: str, *, default: Any = None) -> Any:
    current: Any = payload
    for part in path.split(".") if path else []:
        if not isinstance(current, dict) or part not in current:
            return default
        current = current[part]
    return current


def response_shape(payload: dict[str, Any], *, expected_paths: tuple[str, ...] = ()) -> list[str]:
    """Describe JSON container types without exposing response values."""
    fields: list[str] = []
    safe_keys = SAFE_SHAPE_KEYS | {
        part for path in expected_paths for part in path.split(".") if part
    }

    def visit(value: Any, path: str, depth: int) -> None:
        if len(fields) >= MAX_SHAPE_FIELDS:
            return
        fields.append(f"{path}:{_json_type(value)}")
        if not isinstance(value, dict) or depth >= MAX_SHAPE_DEPTH:
            return
        for raw_key, child in value.items():
            if len(fields) >= MAX_SHAPE_FIELDS:
                return
            key = str(raw_key)
            safe_key = key if key in safe_keys else "<redacted-key>"
            visit(child, f"{path}.{safe_key}", depth + 1)

    visit(payload, "$", 0)
    return fields


def _json_type(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, dict):
        return "object"
    if isinstance(value, list):
        return "array"
    if isinstance(value, str):
        return "string"
    if isinstance(value, (int, float)):
        return "number"
    return "unknown"


def _single_array_collection(payload: dict[str, Any], end_flag_path: str) -> Any:
    """Return a renamed top-level collection only for an unambiguous response shape."""
    if "." in end_flag_path or end_flag_path not in payload or len(payload) != 2:
        return _MISSING
    candidates = [value for key, value in payload.items() if key != end_flag_path]
    if len(candidates) != 1 or not isinstance(candidates[0], list):
        return _MISSING
    return candidates[0]


async def paginate(
    fetch: Callable[[dict[str, int]], Awaitable[dict[str, Any]]],
    override: dict[str, Any],
    *,
    deduplication_field: str | None = None,
    operation_id: str | None = None,
    max_pages: int = 100,
) -> list[dict[str, Any]]:
    skip = 0
    page_size = int(override.get("page_size", 1000))
    all_items: list[dict[str, Any]] = []
    seen_pages: set[str] = set()
    seen_ids: set[str] = set()
    retried_empty_page = False
    for _ in range(max_pages):
        payload = await fetch(
            {
                str(override.get("skip_parameter", "skip")): skip,
                str(override.get("take_parameter", "take")): page_size,
            }
        )
        items_path = str(override.get("items_path", ""))
        end_flag_path = str(override.get("end_flag_path", ""))
        items = get_path(payload, items_path, default=_MISSING)
        if items is _MISSING and override.get("single_array_collection_fallback"):
            items = _single_array_collection(payload, end_flag_path)
            if items is not _MISSING:
                logger.warning(
                    "Dodo pagination used configured single-array fallback "
                    "operation=%s expected=%s",
                    operation_id or "unknown",
                    items_path,
                )
        if not isinstance(items, list):
            shape = response_shape(payload, expected_paths=(items_path, end_flag_path))
            logger.warning(
                "Dodo pagination shape mismatch operation=%s expected=%s shape=%s",
                operation_id or "unknown",
                items_path,
                shape,
            )
            raise DodoApiError(
                f"Dodo IS вернул некорректную коллекцию пагинации «{items_path}»",
                details={
                    "operation_id": operation_id,
                    "expected_items_path": items_path,
                    "response_shape": shape,
                },
            )
        signature = json.dumps(items, sort_keys=True, ensure_ascii=False)
        if signature in seen_pages and items:
            raise DodoApiError("Dodo IS повторяет одну и ту же страницу")
        seen_pages.add(signature)
        for item in items:
            if not isinstance(item, dict):
                continue
            if deduplication_field and item.get(deduplication_field) is not None:
                key = str(item[deduplication_field])
                if key in seen_ids:
                    continue
                seen_ids.add(key)
            all_items.append(item)
        ended = get_path(payload, end_flag_path)
        if not isinstance(ended, bool):
            raise DodoApiError(f"Dodo IS вернул некорректный флаг пагинации «{end_flag_path}»")
        if ended:
            return all_items
        if not items:
            if override.get("empty_page_is_end"):
                if retried_empty_page:
                    return all_items
                retried_empty_page = True
                continue
            raise DodoApiError("Dodo IS вернул пустую страницу без признака завершения")
        retried_empty_page = False
        skip += len(items)
    raise DodoApiError("Превышен лимит страниц Dodo IS")
