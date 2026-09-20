"""VAT calculation utilities for monetary reports."""

from __future__ import annotations

import contextlib
from dataclasses import dataclass
from typing import Any

from app.reports.metric_registry import MetricRegistry


@dataclass(frozen=True)
class VatOptions:
    """Options for VAT calculation in reports."""

    mode: str  # "with_vat" or "without_vat"
    rate: int | None = None  # 10 or 22 (percent)

    def __post_init__(self) -> None:
        if self.mode not in ("with_vat", "without_vat"):
            raise ValueError(f"Invalid VAT mode: {self.mode}")
        if self.mode == "with_vat" and self.rate not in (10, 22):
            raise ValueError(f"Invalid VAT rate: {self.rate}")


def apply_vat_to_aggregated_data(
    data: list[dict[str, Any]],
    metric_ids: list[str],
    vat_options: VatOptions | None,
    metrics: MetricRegistry,
) -> list[dict[str, Any]]:
    """
    Apply VAT transformation to aggregated report data.

    The Dodo IS API returns sales WITHOUT VAT.
    To show with VAT, we multiply by (1 + rate/100).
    For without VAT, values remain unchanged.
    """
    if not vat_options or vat_options.mode == "without_vat":
        return data

    # Check which metrics are monetary
    monetary_metrics = [metric_id for metric_id in metric_ids if metrics.is_monetary(metric_id)]
    if not monetary_metrics:
        return data

    # Calculate multiplier: 1.10 for 10%, 1.22 for 22%
    multiplier = 1 + (vat_options.rate or 0) / 100

    # Apply multiplier to monetary metric values
    result = []
    for row in data:
        new_row = dict(row)
        for metric_id in monetary_metrics:
            if metric_id in new_row and new_row[metric_id] is not None:
                with contextlib.suppress(ValueError, TypeError):
                    new_row[metric_id] = round(float(new_row[metric_id]) * multiplier, 2)
        result.append(new_row)

    return result
