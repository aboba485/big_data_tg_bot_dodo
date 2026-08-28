from __future__ import annotations

import csv
from pathlib import Path
from typing import Any


def export_csv(
    path: Path, columns: list[str], rows: list[dict[str, Any]], totals: dict[str, Any]
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
        if totals:
            total_row = {column: totals.get(column, "") for column in columns}
            if columns:
                total_row[columns[0]] = "Итого"
            writer.writerow(total_row)
