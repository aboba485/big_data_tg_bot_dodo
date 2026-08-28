from __future__ import annotations

from datetime import datetime
from typing import Any


def parse_datetime(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def clipped_interval(
    start: str | datetime,
    end: str | datetime | None,
    boundary_start: datetime,
    boundary_end: datetime,
    *,
    now: datetime | None = None,
) -> tuple[datetime, datetime] | None:
    actual_start = max(parse_datetime(start), boundary_start)
    actual_end = min(
        parse_datetime(end) if end else min(boundary_end, now or datetime.now(boundary_end.tzinfo)),
        boundary_end,
    )
    return (actual_start, actual_end) if actual_end > actual_start else None


def merge_intervals(
    intervals: list[tuple[datetime, datetime]],
) -> list[tuple[datetime, datetime]]:
    if not intervals:
        return []
    ordered = sorted(intervals, key=lambda item: item[0])
    merged = [ordered[0]]
    for start, end in ordered[1:]:
        previous_start, previous_end = merged[-1]
        if start <= previous_end:
            merged[-1] = (previous_start, max(previous_end, end))
        else:
            merged.append((start, end))
    return merged


def stop_duration_hours(
    rows: list[dict[str, Any]],
    boundary_start: datetime,
    boundary_end: datetime,
    *,
    now: datetime | None = None,
) -> float:
    unique: dict[str, dict[str, Any]] = {}
    for index, row in enumerate(rows):
        unique.setdefault(str(row.get("id", index)), row)
    intervals = []
    for row in unique.values():
        start = row.get("startedAtLocal")
        if not start:
            continue
        interval = clipped_interval(
            start,
            row.get("endedAtLocal"),
            boundary_start,
            boundary_end,
            now=now,
        )
        if interval:
            intervals.append(interval)
    return sum((end - start).total_seconds() for start, end in merge_intervals(intervals)) / 3600
