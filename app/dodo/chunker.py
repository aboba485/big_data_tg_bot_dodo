from __future__ import annotations

import calendar
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date, timedelta

from app.planner.schemas import Granularity


@dataclass(frozen=True)
class DateRange:
    start: date
    end: date


def chunk_units(unit_ids: list[str], maximum: int) -> list[list[str]]:
    if maximum <= 0:
        return [unit_ids]
    return [unit_ids[index : index + maximum] for index in range(0, len(unit_ids), maximum)]


def chunk_dates(start: date, end: date, maximum_days: int) -> list[DateRange]:
    if start > end:
        raise ValueError("Начало периода находится после конца")
    if maximum_days <= 0:
        return [DateRange(start, end)]
    result = []
    cursor = start
    while cursor <= end:
        chunk_end = min(end, cursor + timedelta(days=maximum_days - 1))
        result.append(DateRange(cursor, chunk_end))
        cursor = chunk_end + timedelta(days=1)
    return result


def report_buckets(start: date, end: date, granularity: Granularity) -> list[DateRange]:
    if granularity == Granularity.TOTAL:
        return [DateRange(start, end)]
    result = []
    cursor = start
    while cursor <= end:
        if granularity == Granularity.DAY:
            bucket_end = cursor
        elif granularity == Granularity.WEEK:
            bucket_end = min(end, cursor + timedelta(days=6 - cursor.weekday()))
        else:
            bucket_end = min(
                end,
                date(cursor.year, cursor.month, calendar.monthrange(cursor.year, cursor.month)[1]),
            )
        result.append(DateRange(cursor, bucket_end))
        cursor = bucket_end + timedelta(days=1)
    return result


def execution_ranges(
    start: date, end: date, granularity: Granularity, maximum_days: int
) -> Iterator[tuple[DateRange, DateRange]]:
    for bucket in report_buckets(start, end, granularity):
        for chunk in chunk_dates(bucket.start, bucket.end, maximum_days):
            yield bucket, chunk
