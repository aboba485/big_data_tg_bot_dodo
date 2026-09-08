from __future__ import annotations

import calendar
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from app.storage.sqlite import SQLiteDatabase


def parse_subscription_unit_ids(value: str) -> list[str]:
    text = value.strip()
    if not text:
        return []
    if text.startswith("["):
        parsed = json.loads(text)
        if not isinstance(parsed, list):
            raise ValueError("Некорректный список заведений подписки")
        return [str(item) for item in parsed if str(item).strip()]
    return [text]


def encode_subscription_unit_ids(unit_ids: list[str]) -> str:
    unique = sorted(dict.fromkeys(unit_ids))
    if not unique:
        raise ValueError("Не выбраны заведения")
    if len(unique) == 1:
        return unique[0]
    return json.dumps(unique, ensure_ascii=False)


@dataclass(frozen=True)
class ScheduledReportSubscription:
    """A scheduled report subscription (weekly or monthly)."""

    id: int
    telegram_id: int
    chat_id: int
    metric_id: str
    unit_id: str
    output_format: str
    weekday: int
    local_hour: int
    timezone: str
    next_run_at: datetime
    enabled: bool = True
    granularity: str = "total"
    spreadsheet_id: str | None = None
    frequency: str = "weekly"
    day_of_month: int | None = None
    sales_channel_choice: str = ""


# Alias for backward compatibility
WeeklyReportSubscription = ScheduledReportSubscription


class WeeklyReportLimitError(ValueError):
    pass


class ScheduledReportLimitError(WeeklyReportLimitError):
    pass


def next_weekly_run(
    now_utc: datetime, weekday: int, local_hour: int, timezone_name: str
) -> datetime:
    zone = ZoneInfo(timezone_name)
    local_now = now_utc.astimezone(zone)
    days = (weekday - local_now.weekday()) % 7
    candidate = (local_now + timedelta(days=days)).replace(
        hour=local_hour, minute=0, second=0, microsecond=0
    )
    if candidate <= local_now:
        candidate += timedelta(days=7)
    return candidate.astimezone(UTC)


def next_monthly_run(
    now_utc: datetime, day_of_month: int, local_hour: int, timezone_name: str
) -> datetime:
    """Calculate the next monthly run time.

    Args:
        day_of_month: Day of the month (1-28). Values > 28 are clamped to the last day.
    """
    zone = ZoneInfo(timezone_name)
    local_now = now_utc.astimezone(zone)

    year = local_now.year
    month = local_now.month
    last_day = calendar.monthrange(year, month)[1]
    target_day = min(day_of_month, last_day)

    candidate = local_now.replace(
        day=target_day, hour=local_hour, minute=0, second=0, microsecond=0
    )

    if candidate <= local_now:
        if month == 12:
            year += 1
            month = 1
        else:
            month += 1
        last_day = calendar.monthrange(year, month)[1]
        target_day = min(day_of_month, last_day)
        candidate = candidate.replace(year=year, month=month, day=target_day)

    return candidate.astimezone(UTC)


def next_scheduled_run(
    now_utc: datetime,
    frequency: str,
    weekday: int,
    day_of_month: int | None,
    local_hour: int,
    timezone_name: str,
) -> datetime:
    """Calculate the next run time based on frequency."""
    if frequency == "monthly" and day_of_month is not None:
        return next_monthly_run(now_utc, day_of_month, local_hour, timezone_name)
    return next_weekly_run(now_utc, weekday, local_hour, timezone_name)


class WeeklyReportRepository:
    def __init__(self, database: SQLiteDatabase) -> None:
        self.database = database

    def create(
        self,
        *,
        telegram_id: int,
        chat_id: int,
        metric_id: str,
        unit_id: str,
        output_format: str,
        weekday: int,
        local_hour: int,
        timezone_name: str,
        granularity: str = "total",
        frequency: str = "weekly",
        day_of_month: int | None = None,
        sales_channel_choice: str = "",
        now_utc: datetime | None = None,
        max_per_user: int = 10,
    ) -> ScheduledReportSubscription:
        now = now_utc or datetime.now(UTC)
        next_run = next_scheduled_run(
            now, frequency, weekday, day_of_month, local_hour, timezone_name
        )
        stamp = now.isoformat()
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if frequency == "monthly":
                existing = connection.execute(
                    """SELECT id FROM weekly_report_subscriptions
                    WHERE telegram_id=? AND metric_id=? AND unit_id=? AND output_format=?
                      AND granularity=? AND sales_channel_choice=? AND frequency=?
                      AND day_of_month=? AND local_hour=?
                      AND timezone=? AND enabled=1""",
                    (
                        telegram_id,
                        metric_id,
                        unit_id,
                        output_format,
                        granularity,
                        sales_channel_choice,
                        frequency,
                        day_of_month,
                        local_hour,
                        timezone_name,
                    ),
                ).fetchone()
            else:
                existing = connection.execute(
                    """SELECT id FROM weekly_report_subscriptions
                    WHERE telegram_id=? AND metric_id=? AND unit_id=? AND output_format=?
                      AND granularity=? AND sales_channel_choice=? AND frequency=?
                      AND weekday=? AND local_hour=?
                      AND timezone=? AND enabled=1""",
                    (
                        telegram_id,
                        metric_id,
                        unit_id,
                        output_format,
                        granularity,
                        sales_channel_choice,
                        frequency,
                        weekday,
                        local_hour,
                        timezone_name,
                    ),
                ).fetchone()
            if existing:
                subscription_id = int(existing["id"])
            else:
                count = int(
                    connection.execute(
                        """SELECT COUNT(*) FROM weekly_report_subscriptions
                        WHERE telegram_id=? AND enabled=1""",
                        (telegram_id,),
                    ).fetchone()[0]
                )
                if count >= max_per_user:
                    raise ScheduledReportLimitError(
                        f"Можно сохранить не более {max_per_user} запланированных отчётов."
                    )
                cursor = connection.execute(
                    """INSERT INTO weekly_report_subscriptions
                    (telegram_id, chat_id, metric_id, unit_id, output_format, granularity,
                     sales_channel_choice, weekday, local_hour, timezone, next_run_at, frequency,
                     day_of_month, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        telegram_id,
                        chat_id,
                        metric_id,
                        unit_id,
                        output_format,
                        granularity,
                        sales_channel_choice,
                        weekday,
                        local_hour,
                        timezone_name,
                        next_run.isoformat(),
                        frequency,
                        day_of_month,
                        stamp,
                        stamp,
                    ),
                )
                subscription_id = int(cursor.lastrowid)
        return self.get(subscription_id)  # type: ignore[return-value]

    def get(self, subscription_id: int) -> ScheduledReportSubscription | None:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM weekly_report_subscriptions WHERE id = ?", (subscription_id,)
            ).fetchone()
        return self._from_row(row) if row else None

    def list_for_user(self, telegram_id: int) -> list[ScheduledReportSubscription]:
        with self.database.connect() as connection:
            rows = connection.execute(
                """SELECT * FROM weekly_report_subscriptions
                WHERE telegram_id = ? AND enabled = 1 ORDER BY id""",
                (telegram_id,),
            ).fetchall()
        return [self._from_row(row) for row in rows]

    def disable(self, subscription_id: int, now_utc: datetime | None = None) -> bool:
        stamp = (now_utc or datetime.now(UTC)).isoformat()
        with self.database.connect() as connection:
            cursor = connection.execute(
                """UPDATE weekly_report_subscriptions SET enabled=0, updated_at=?
                WHERE id=? AND enabled=1""",
                (stamp, subscription_id),
            )
        return cursor.rowcount == 1

    def delete_for_user(self, subscription_id: int, telegram_id: int) -> bool:
        with self.database.connect() as connection:
            cursor = connection.execute(
                "DELETE FROM weekly_report_subscriptions WHERE id = ? AND telegram_id = ?",
                (subscription_id, telegram_id),
            )
        return cursor.rowcount == 1

    def due(self, now_utc: datetime) -> list[ScheduledReportSubscription]:
        with self.database.connect() as connection:
            rows = connection.execute(
                """SELECT * FROM weekly_report_subscriptions
                WHERE enabled = 1 AND next_run_at <= ? ORDER BY next_run_at LIMIT 50""",
                (now_utc.isoformat(),),
            ).fetchall()
        return [self._from_row(row) for row in rows]

    def claim(self, subscription_id: int, period_end: str, now_utc: datetime) -> bool:
        with self.database.connect() as connection:
            cursor = connection.execute(
                """INSERT OR IGNORE INTO weekly_report_deliveries
                (subscription_id, period_end, status, claimed_at)
                VALUES (?, ?, 'processing', ?)""",
                (subscription_id, period_end, now_utc.isoformat()),
            )
            if cursor.rowcount == 0:
                cursor = connection.execute(
                    """UPDATE weekly_report_deliveries
                    SET status='processing', claimed_at=?, error_code=NULL
                    WHERE subscription_id=? AND period_end=?
                      AND (status='failed' OR (status='processing' AND claimed_at<=?))""",
                    (
                        now_utc.isoformat(),
                        subscription_id,
                        period_end,
                        (now_utc - timedelta(minutes=30)).isoformat(),
                    ),
                )
        return cursor.rowcount == 1

    def mark_sent(
        self, subscription: ScheduledReportSubscription, period_end: str, now: datetime
    ) -> None:
        next_run = next_scheduled_run(
            now + timedelta(seconds=1),
            subscription.frequency,
            subscription.weekday,
            subscription.day_of_month,
            subscription.local_hour,
            subscription.timezone,
        )
        with self.database.connect() as connection:
            connection.execute(
                """UPDATE weekly_report_deliveries SET status='sent', sent_at=?
                WHERE subscription_id=? AND period_end=?""",
                (now.isoformat(), subscription.id, period_end),
            )
            connection.execute(
                """UPDATE weekly_report_subscriptions SET next_run_at=?, updated_at=?
                WHERE id=?""",
                (next_run.isoformat(), now.isoformat(), subscription.id),
            )

    def mark_failed(
        self, subscription: ScheduledReportSubscription, period_end: str, now: datetime
    ) -> None:
        with self.database.connect() as connection:
            connection.execute(
                """UPDATE weekly_report_deliveries SET status='failed'
                WHERE subscription_id=? AND period_end=?""",
                (subscription.id, period_end),
            )
            connection.execute(
                """UPDATE weekly_report_subscriptions SET next_run_at=?, updated_at=?
                WHERE id=?""",
                ((now + timedelta(minutes=15)).isoformat(), now.isoformat(), subscription.id),
            )

    def update_spreadsheet_id(
        self, subscription_id: int, spreadsheet_id: str, now_utc: datetime | None = None
    ) -> bool:
        """Store the Google Sheets spreadsheet ID for appending future reports."""
        stamp = (now_utc or datetime.now(UTC)).isoformat()
        with self.database.connect() as connection:
            cursor = connection.execute(
                """UPDATE weekly_report_subscriptions
                SET spreadsheet_id=?, updated_at=?
                WHERE id=?""",
                (spreadsheet_id, stamp, subscription_id),
            )
        return cursor.rowcount == 1

    @staticmethod
    def _from_row(row) -> ScheduledReportSubscription:
        keys = set(row.keys())
        return ScheduledReportSubscription(
            id=int(row["id"]),
            telegram_id=int(row["telegram_id"]),
            chat_id=int(row["chat_id"]),
            metric_id=str(row["metric_id"]),
            unit_id=str(row["unit_id"]),
            output_format=str(row["output_format"]),
            weekday=int(row["weekday"]),
            local_hour=int(row["local_hour"]),
            timezone=str(row["timezone"]),
            next_run_at=datetime.fromisoformat(row["next_run_at"]),
            enabled=bool(row["enabled"]),
            granularity=str(row["granularity"]) if "granularity" in keys else "total",
            spreadsheet_id=str(row["spreadsheet_id"])
            if "spreadsheet_id" in keys and row["spreadsheet_id"]
            else None,
            frequency=str(row["frequency"])
            if "frequency" in keys and row["frequency"]
            else "weekly",
            day_of_month=int(row["day_of_month"])
            if "day_of_month" in keys and row["day_of_month"] is not None
            else None,
            sales_channel_choice=str(row["sales_channel_choice"])
            if "sales_channel_choice" in keys and row["sales_channel_choice"]
            else "",
        )
