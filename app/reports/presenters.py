from __future__ import annotations

from typing import Any


def present_value(metric_id: str, value: Any) -> Any:
    if value is None:
        return None
    if not isinstance(value, (int, float)):
        return value
    if metric_id.endswith("_count") or metric_id in {"orders_count", "vouchers_count"}:
        return int(round(value))
    if "seconds" in metric_id:
        return round(float(value), 2)
    return round(float(value), 2)


def deterministic_summary(rows: list[dict[str, Any]], metric_ids: list[str]) -> str:
    if not rows:
        return "За выбранный период данных не найдено."
    names = ", ".join(metric_ids)
    return f"Сформировано строк: {len(rows)}. Метрики: {names}."
