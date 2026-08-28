from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from openpyxl import Workbook
from openpyxl.styles import Font
from openpyxl.utils import get_column_letter

from app.planner.schemas import ReportPlan


def export_xlsx(
    path: Path,
    *,
    query: str,
    plan: ReportPlan,
    unit_ids: list[str],
    columns: list[str],
    rows: list[dict[str, Any]],
    totals: dict[str, Any],
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Report"
    sheet.append(["Отчёт Dodo IS"])
    sheet["A1"].font = Font(bold=True, size=16)
    metadata = [
        ("Запрос", query),
        ("Период", f"{plan.date_from} — {plan.date_to}"),
        ("Заведения", ", ".join(unit_ids)),
        ("Метрики", ", ".join(plan.metric_ids)),
        ("Endpoint IDs", ", ".join(plan.operation_ids)),
        ("Сформирован", datetime.now().isoformat(timespec="seconds")),
    ]
    for key, value in metadata:
        sheet.append([key, value])
    header_row = sheet.max_row + 2
    sheet.append([])
    sheet.append(columns)
    for cell in sheet[header_row]:
        cell.font = Font(bold=True)
    for row in rows:
        sheet.append([row.get(column) for column in columns])
    if totals:
        sheet.append(["Итого", *[totals.get(column) for column in columns[1:]]])
        for cell in sheet[sheet.max_row]:
            cell.font = Font(bold=True)
    sheet.freeze_panes = f"A{header_row + 1}"
    if columns:
        sheet.auto_filter.ref = (
            f"A{header_row}:{get_column_letter(len(columns))}{max(header_row, sheet.max_row - 1)}"
        )
    for index, column in enumerate(columns, 1):
        values = [str(column), *(str(row.get(column, "")) for row in rows[:200])]
        sheet.column_dimensions[get_column_letter(index)].width = min(
            max(len(value) for value in values) + 2, 45
        )
    workbook.save(path)
