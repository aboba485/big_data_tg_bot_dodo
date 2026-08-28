from __future__ import annotations

from collections.abc import Iterable
from decimal import Decimal
from typing import Any


def sum_values(rows: Iterable[dict[str, Any]], field: str) -> float:
    return float(sum(Decimal(str(row.get(field) or 0)) for row in rows))


def count_values(rows: Iterable[dict[str, Any]], field: str | None = None) -> int:
    return sum(1 for row in rows if field is None or row.get(field) is not None)


def count_unique(rows: Iterable[dict[str, Any]], field: str) -> int:
    return len({str(row[field]) for row in rows if row.get(field) is not None})


def weighted_average(
    rows: Iterable[dict[str, Any]], value_field: str, weight_field: str
) -> float | None:
    numerator = Decimal(0)
    denominator = Decimal(0)
    for row in rows:
        if row.get(value_field) is None:
            continue
        weight = Decimal(str(row.get(weight_field) or 0))
        numerator += Decimal(str(row[value_field])) * weight
        denominator += weight
    return float(numerator / denominator) if denominator else None


def average_values(rows: Iterable[dict[str, Any]], field: str) -> float | None:
    values = [Decimal(str(row[field])) for row in rows if row.get(field) is not None]
    return float(sum(values) / len(values)) if values else None


def ratio_from_sums(
    rows: Iterable[dict[str, Any]],
    numerator_field: str,
    denominator_field: str,
    multiplier: float = 1.0,
) -> float | None:
    items = list(rows)
    denominator = sum_values(items, denominator_field)
    return sum_values(items, numerator_field) / denominator * multiplier if denominator else None
