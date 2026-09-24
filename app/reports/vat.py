"""VAT calculation utilities for monetary reports."""

from __future__ import annotations

import contextlib
from dataclasses import dataclass
from typing import Any

from app.dodo.channels import sales_channel_key
from app.reports.metric_registry import MetricRegistry

DELIVERY_VAT_RATE = 12.2
DINE_IN_VAT_RATE = 14.5

# Takeout uses the Dine-in rate. Keys match sales_channel_key().
_CHANNEL_VAT_RATES = {
    "delivery": DELIVERY_VAT_RATE,
    "dinein": DINE_IN_VAT_RATE,
    "takeaway": DINE_IN_VAT_RATE,
    "takeout": DINE_IN_VAT_RATE,
}


@dataclass(frozen=True)
class VatOptions:
    """Options for VAT calculation in reports."""

    mode: str  # "with_vat" or "without_vat"

    def __post_init__(self) -> None:
        if self.mode not in ("with_vat", "without_vat"):
            raise ValueError(f"Invalid VAT mode: {self.mode}")


def vat_rate_for_channel(channel: str | None) -> float | None:
    """Return the VAT percent for a sales channel, or None if unknown."""
    if not channel:
        return None
    return _CHANNEL_VAT_RATES.get(sales_channel_key(channel))


def apply_vat_to_aggregated_data(
    data: list[dict[str, Any]],
    metric_ids: list[str],
    vat_options: VatOptions | None,
    metrics: MetricRegistry,
    *,
    default_channel: str | None = None,
) -> list[dict[str, Any]]:
    """
    Apply VAT transformation to aggregated report data.

    The Dodo IS API returns sales WITHOUT VAT.
    To show with VAT, monetary values are multiplied by (1 + rate/100)
    using the row's sales channel: Delivery 12.2%, Dine-in/Takeaway 14.5%.
    Without VAT, values remain unchanged.
    """
    if not vat_options or vat_options.mode == "without_vat":
        return data

    monetary_metrics = [metric_id for metric_id in metric_ids if metrics.is_monetary(metric_id)]
    if not monetary_metrics:
        return data

    result = []
    for row in data:
        new_row = dict(row)
        channel = str(new_row.get("salesChannel") or default_channel or "").strip() or None
        rate = vat_rate_for_channel(channel)
        if rate is None:
            result.append(new_row)
            continue
        multiplier = 1 + rate / 100
        for metric_id in monetary_metrics:
            if metric_id in new_row and new_row[metric_id] is not None:
                with contextlib.suppress(ValueError, TypeError):
                    new_row[metric_id] = round(float(new_row[metric_id]) * multiplier, 2)
        result.append(new_row)

    return result


def collapse_channel_rows(
    rows: list[dict[str, Any]],
    columns: list[str],
    value_fields: list[str],
) -> tuple[list[dict[str, Any]], list[str]]:
    """Merge per-channel rows into combined totals after VAT is applied."""
    if not any(row.get("salesChannel") for row in rows) and "salesChannel" not in columns:
        return rows, columns

    new_columns = [column for column in columns if column != "salesChannel"]
    value_set = set(value_fields)
    grouped: dict[tuple[Any, ...], dict[str, Any]] = {}
    order: list[tuple[Any, ...]] = []
    for row in rows:
        key = tuple(row.get(column) for column in new_columns if column not in value_set)
        if key not in grouped:
            merged = {column: row.get(column) for column in new_columns}
            grouped[key] = merged
            order.append(key)
            continue
        merged = grouped[key]
        for field in value_fields:
            left = merged.get(field)
            right = row.get(field)
            if left is None:
                merged[field] = right
            elif right is not None:
                with contextlib.suppress(ValueError, TypeError):
                    merged[field] = round(float(left) + float(right), 2)
    return [grouped[key] for key in order], new_columns


def sum_metric_totals(rows: list[dict[str, Any]], metric_ids: list[str]) -> dict[str, Any]:
    """Sum metric columns from rows for report totals."""
    totals: dict[str, Any] = {}
    for metric_id in metric_ids:
        values: list[float] = []
        for row in rows:
            value = row.get(metric_id)
            if value is None:
                continue
            with contextlib.suppress(ValueError, TypeError):
                values.append(float(value))
        if values:
            totals[metric_id] = round(sum(values), 2)
    return totals
