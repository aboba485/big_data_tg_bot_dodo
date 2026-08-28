from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from aiogram import Bot
from aiogram.types import FSInputFile

from app.bot.models import BotReportResult
from app.bot.services import BotReportService
from app.errors import GoogleDriveError
from app.planner.schemas import Granularity, OutputFormat
from app.storage.weekly_reports import (
    ScheduledReportSubscription,
    WeeklyReportRepository,
    parse_subscription_unit_ids,
)

logger = logging.getLogger(__name__)


class WeeklyReportScheduler:
    def __init__(
        self,
        bot: Bot,
        repository: WeeklyReportRepository,
        report_service: BotReportService,
        poll_seconds: int,
    ) -> None:
        self.bot = bot
        self.repository = repository
        self.report_service = report_service
        self.poll_seconds = poll_seconds
        self._stopped = asyncio.Event()

    async def run(self) -> None:
        while not self._stopped.is_set():
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("weekly_scheduler_iteration_failed")
            with suppress(TimeoutError):
                await asyncio.wait_for(self._stopped.wait(), timeout=self.poll_seconds)

    def stop(self) -> None:
        self._stopped.set()

    async def run_once(self, now_utc: datetime | None = None) -> None:
        now = now_utc or datetime.now(UTC)
        for subscription in self.repository.due(now):
            try:
                await self._deliver(subscription, now)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "weekly_subscription_iteration_failed subscription_id=%s",
                    subscription.id,
                )

    async def _deliver(self, subscription: ScheduledReportSubscription, now: datetime) -> None:
        local_today = now.astimezone(ZoneInfo(subscription.timezone)).date()
        date_from, date_to = self._calculate_period(subscription.frequency, local_today)
        period_end = date_to.isoformat()
        report_label = (
            "Ежемесячный отчёт" if subscription.frequency == "monthly" else "Еженедельный отчёт"
        )  # Keep internal labels for delivery context

        if not self.repository.claim(subscription.id, period_end, now):
            return
        current = self.repository.get(subscription.id)
        if current is None:
            return
        subscription = current
        try:
            user = self.report_service.user_access.authorized(subscription.telegram_id)
            if user is None:
                raise PermissionError("Доступ пользователя отозван")
            if subscription.output_format == OutputFormat.SHEETS.value and not (
                self.report_service.sheets_available(user)
            ):
                await self._disable_unusable(subscription, period_end, now)
                return

            is_sheets = subscription.output_format == OutputFormat.SHEETS.value
            run_format = (
                OutputFormat.TABLE if is_sheets else OutputFormat(subscription.output_format)
            )

            result = await self.report_service.run_selected(
                user,
                metric_id=subscription.metric_id,
                unit_ids=parse_subscription_unit_ids(subscription.unit_id),
                date_from=date_from,
                date_to=date_to,
                output_format=run_format,
                granularity=Granularity(subscription.granularity or Granularity.TOTAL.value),
            )
            if result.status != "ready":
                raise RuntimeError(result.reason or result.question or "Отчёт не сформирован")

            if is_sheets:
                sheet_url = await self._deliver_to_sheets(
                    subscription, result, date_from, date_to, now
                )
                await self.bot.send_message(
                    subscription.chat_id,
                    f"Google Sheets: {sheet_url}\n\n{result.text or report_label}",
                )
            elif result.file_path and result.report_id:
                try:
                    await self.bot.send_document(
                        subscription.chat_id,
                        FSInputFile(result.file_path, filename=result.file_name),
                        caption=(result.text or report_label)[:1000],
                    )
                finally:
                    self.report_service.cleanup_file(result.report_id)
            else:
                await self.bot.send_message(
                    subscription.chat_id, result.text or f"{report_label} сформирован."
                )
        except asyncio.CancelledError:
            self.repository.mark_failed(subscription, period_end, now)
            raise
        except Exception:
            logger.exception(
                "scheduled_report_failed subscription_id=%s telegram_id=%s frequency=%s",
                subscription.id,
                subscription.telegram_id,
                subscription.frequency,
            )
            self.repository.mark_failed(subscription, period_end, now)
            return
        self.repository.mark_sent(subscription, period_end, now)

    @staticmethod
    def _calculate_period(frequency: str, local_today: date) -> tuple[date, date]:
        """Calculate the report period based on frequency.

        For weekly: previous completed week (Mon-Sun)
        For monthly: previous completed month
        """
        if frequency == "monthly":
            first_of_current_month = local_today.replace(day=1)
            date_to = first_of_current_month - timedelta(days=1)
            date_from = date_to.replace(day=1)
        else:
            current_monday = local_today - timedelta(days=local_today.weekday())
            date_to = current_monday - timedelta(days=1)
            date_from = date_to - timedelta(days=6)
        return date_from, date_to

    async def _deliver_to_sheets(
        self,
        subscription: ScheduledReportSubscription,
        result: BotReportResult,
        date_from: date,
        date_to: date,
        now: datetime,
    ) -> str:
        """Create or append to Google Sheets for a scheduled subscription.

        Returns the spreadsheet URL.
        """
        google_drive = self.report_service.google_drive
        if google_drive is None:
            raise RuntimeError("Google Drive service not configured")

        response = result.response or {}
        columns = list(response.get("columns") or [])
        rows = list(response.get("rows") or [])
        totals = dict(response.get("totals") or {})
        period_label = f"{date_from} — {date_to}"

        if subscription.spreadsheet_id:
            try:
                url = await google_drive.append_to_spreadsheet(
                    subscription.telegram_id,
                    subscription.spreadsheet_id,
                    period_label=period_label,
                    columns=columns,
                    rows=rows,
                    totals=totals,
                )
                return url
            except GoogleDriveError as exc:
                logger.warning(
                    "scheduled_sheet_append_failed subscription_id=%s spreadsheet_id=%s error=%s",
                    subscription.id,
                    subscription.spreadsheet_id,
                    exc.message,
                )

        plan = response.get("plan") or {}
        names = list(plan.get("metrics") or []) or list(plan.get("operations") or [])
        freq_label = "Ежемесячный" if subscription.frequency == "monthly" else "Еженедельный"
        title = ", ".join(str(name) for name in names) or f"{freq_label} отчёт"
        title = f"{title} (авто)"

        url, spreadsheet_id = await google_drive.create_spreadsheet(
            subscription.telegram_id,
            title=title,
            columns=columns,
            rows=rows,
            totals=totals,
            period_label=period_label,
        )
        self.repository.update_spreadsheet_id(subscription.id, spreadsheet_id, now)
        logger.info(
            "scheduled_sheet_created subscription_id=%s spreadsheet_id=%s frequency=%s",
            subscription.id,
            spreadsheet_id,
            subscription.frequency,
        )
        return url

    async def _disable_unusable(
        self, subscription: ScheduledReportSubscription, period_end: str, now: datetime
    ) -> None:
        """Stop retrying a Google Sheets subscription whose Drive link no longer exists."""
        logger.info(
            "scheduled_subscription_disabled subscription_id=%s reason=google_drive_unlinked",
            subscription.id,
        )
        self.repository.mark_failed(subscription, period_end, now)
        self.repository.disable(subscription.id, now)
        freq_label = "ежемесячный" if subscription.frequency == "monthly" else "еженедельный"
        with suppress(Exception):
            await self.bot.send_message(
                subscription.chat_id,
                f"Повторяющийся ({freq_label}) отчёт #{subscription.id} отключён: "
                "Google Drive больше не подключён. Подключите аккаунт через /drive "
                "и создайте подписку заново.",
            )
