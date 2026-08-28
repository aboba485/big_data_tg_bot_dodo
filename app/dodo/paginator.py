from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import Any

from app.errors import DodoApiError


def get_path(payload: dict[str, Any], path: str) -> Any:
    current: Any = payload
    for part in path.split(".") if path else []:
        if not isinstance(current, dict):
            return None
        current = current.get(part)
    return current


async def paginate(
    fetch: Callable[[dict[str, int]], Awaitable[dict[str, Any]]],
    override: dict[str, Any],
    *,
    deduplication_field: str | None = None,
    max_pages: int = 100,
) -> list[dict[str, Any]]:
    skip = 0
    page_size = int(override.get("page_size", 1000))
    all_items: list[dict[str, Any]] = []
    seen_pages: set[str] = set()
    seen_ids: set[str] = set()
    for _ in range(max_pages):
        payload = await fetch(
            {
                str(override.get("skip_parameter", "skip")): skip,
                str(override.get("take_parameter", "take")): page_size,
            }
        )
        items = get_path(payload, str(override.get("items_path", ""))) or []
        if not isinstance(items, list):
            raise DodoApiError("Пагинация получила неожиданный список записей")
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
        ended = bool(get_path(payload, str(override.get("end_flag_path", ""))))
        if ended:
            return all_items
        if not items:
            raise DodoApiError("Dodo IS вернул пустую незавершённую страницу")
        skip += len(items) or page_size
    raise DodoApiError("Превышен лимит страниц Dodo IS")
