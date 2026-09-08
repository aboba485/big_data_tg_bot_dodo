from __future__ import annotations

import math
from datetime import datetime
from typing import Any

TOTAL_LABEL = "Итого"
TIME_FIELDS = ("hour", "day", "week", "month")
ENTITY_FIELDS = ("unitName", "unit_name", "unitId", "unit_id")
EXTRA_FIELDS = ("salesChannel", "ingredient", "ingredientCategory", "stopReason")
DIMENSION_FIELDS = frozenset((*TIME_FIELDS, *ENTITY_FIELDS, *EXTRA_FIELDS))


def is_total_label(value: Any) -> bool:
    return str(value or "").startswith(TOTAL_LABEL)


def period_label_from_dates(date_from: Any, date_to: Any) -> str | None:
    if date_from and date_to:
        return f"{date_from} — {date_to}"
    return None


def cell_value(value: Any) -> Any:
    if value is None:
        return ""
    if isinstance(value, (bool, str)):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    return str(value)


def build_matrix(
    columns: list[str],
    rows: list[dict[str, Any]],
    totals: dict[str, Any] | None = None,
    *,
    period_label: str | None = None,
) -> list[list[Any]] | None:
    value_fields = [column for column in columns if column not in DIMENSION_FIELDS]
    has_entity = any(field in columns for field in ENTITY_FIELDS)
    if not value_fields or not has_entity:
        return None

    multi = len(value_fields) > 1
    default_header = _period_header(period_label) or str(value_fields[0])
    date_order: list[str] = []
    date_seen: set[str] = set()
    unit_order: list[str] = []
    unit_seen: set[str] = set()
    cells: dict[tuple[str, str], Any] = {}

    for row in rows:
        unit = _row_label(row)
        date_header = _row_date_header(row) or default_header
        if date_header not in date_seen:
            date_seen.add(date_header)
            date_order.append(date_header)
        metrics = value_fields if multi else value_fields[:1]
        for metric in metrics:
            label = f"{unit} / {metric}" if multi else unit
            if label not in unit_seen:
                unit_seen.add(label)
                unit_order.append(label)
            cells[(label, date_header)] = cell_value(row.get(metric))

    if not date_order:
        date_order = [default_header]
    if not unit_order:
        unit_order = [f"Все заведения / {value_fields[0]}"] if multi else ["Все заведения"]

    header: list[Any] = ["", *date_order]
    values: list[list[Any]] = [header]
    for unit in unit_order:
        line: list[Any] = [unit]
        for date_header in date_order:
            line.append(cells.get((unit, date_header), ""))
        values.append(line)

    report_totals = totals or {}
    if multi:
        for metric in value_fields:
            values.append(
                _total_row(
                    f"{TOTAL_LABEL} / {metric}",
                    metric,
                    date_order,
                    unit_order,
                    cells,
                    report_totals,
                    multi=True,
                )
            )
    else:
        values.append(
            _total_row(
                TOTAL_LABEL,
                value_fields[0],
                date_order,
                unit_order,
                cells,
                report_totals,
                multi=False,
            )
        )
    return values


def build_table(
    columns: list[str],
    rows: list[dict[str, Any]],
    totals: dict[str, Any] | None = None,
    *,
    period_label: str | None = None,
) -> list[list[Any]]:
    matrix = build_matrix(columns, rows, totals, period_label=period_label)
    if matrix is not None:
        return matrix
    values: list[list[Any]] = [[str(column) for column in columns]]
    values.extend([cell_value(row.get(column)) for column in columns] for row in rows)
    if totals:
        total_row = [cell_value(totals.get(column)) for column in columns]
        if total_row:
            total_row[0] = TOTAL_LABEL
        values.append(total_row)
    return values


def _total_row(
    label: str,
    metric: str,
    date_order: list[str],
    unit_order: list[str],
    cells: dict[tuple[str, str], Any],
    report_totals: dict[str, Any],
    *,
    multi: bool,
) -> list[Any]:
    if len(date_order) == 1 and metric in report_totals:
        return [label, cell_value(report_totals.get(metric))]
    row: list[Any] = [label]
    suffix = f" / {metric}"
    for date_header in date_order:
        total = 0.0
        has_numeric = False
        for unit in unit_order:
            if multi and not unit.endswith(suffix):
                continue
            number = _numeric(cells.get((unit, date_header), ""))
            if number is not None:
                total += number
                has_numeric = True
        row.append(_whole_or_float(total) if has_numeric else "")
    return row


def _row_label(row: dict[str, Any]) -> str:
    name = ""
    for field in ENTITY_FIELDS:
        name = str(row.get(field) or "").strip()
        if name:
            break
    extras = [
        str(row.get(field) or "").strip()
        for field in EXTRA_FIELDS
        if str(row.get(field) or "").strip()
    ]
    parts = [part for part in [name, *extras] if part]
    return " / ".join(parts) or "Все заведения"


def _row_date_header(row: dict[str, Any]) -> str:
    for field in TIME_FIELDS:
        raw = row.get(field)
        if raw not in {None, ""}:
            return _format_date_header(raw)
    return ""


def _period_header(period_label: str | None) -> str:
    if not period_label:
        return ""
    text = period_label.strip()
    for separator in (" — ", " – ", " - "):
        if separator in text:
            text = text.rsplit(separator, 1)[-1].strip()
            break
    return _format_date_header(text) or text


def _format_date_header(value: Any) -> str:
    text = str(value).strip()
    if not text:
        return ""
    if "T" in text:
        date_part, time_part = text.split("T", 1)
        formatted = _iso_date_to_display(date_part)
        hour = time_part[:5] if len(time_part) >= 5 else ""
        return f"{formatted} {hour}".strip() if formatted else text
    formatted = _iso_date_to_display(text[:10])
    return formatted or text


def _iso_date_to_display(value: str) -> str:
    try:
        return datetime.strptime(value, "%Y-%m-%d").strftime("%d.%m.%Y")
    except ValueError:
        return ""


def _numeric(value: Any) -> float | None:
    if isinstance(value, bool) or value in {"", None}:
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        return number if math.isfinite(number) else None
    return None


def _whole_or_float(total: float) -> int | float:
    rounded = round(total, 2)
    if rounded == int(rounded):
        return int(rounded)
    return rounded
