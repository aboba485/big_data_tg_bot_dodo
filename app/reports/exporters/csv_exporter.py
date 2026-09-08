from __future__ import annotations

import csv
from pathlib import Path
from typing import Any

from app.reports.matrix import build_table


def export_csv(
    path: Path,
    columns: list[str],
    rows: list[dict[str, Any]],
    totals: dict[str, Any],
    *,
    period_label: str | None = None,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    values = build_table(columns, rows, totals, period_label=period_label)
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerows(values)
