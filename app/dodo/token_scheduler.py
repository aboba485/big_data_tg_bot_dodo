from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from aiogram import Bot

from app.config import Settings
from app.dodo.oauth import DodoTokenRefresher

logger = logging.getLogger(__name__)

MAX_RETRY_ATTEMPTS = 5
INITIAL_RETRY_DELAY_SECONDS = 60
MAX_RETRY_DELAY_SECONDS = 3600


class DodoTokenRefreshScheduler:
    def __init__(
        self,
        settings: Settings,
        refresher: DodoTokenRefresher,
        bot: Bot | None = None,
    ) -> None:
        self.settings = settings
        self.refresher = refresher
        self.bot = bot
        self._stopped = asyncio.Event()
        self._tz = ZoneInfo(settings.app_timezone)

    def _refresh_time(self) -> time:
        return time(
            hour=self.settings.dodo_token_refresh_hour,
            minute=self.settings.dodo_token_refresh_minute,
        )

    def _next_refresh_at(self, now: datetime) -> datetime:
        """Calculate the next scheduled refresh time in local timezone."""
        local_now = now.astimezone(self._tz)
        refresh_time = self._refresh_time()
        local_today = local_now.date()
        scheduled_today = datetime.combine(local_today, refresh_time, tzinfo=self._tz)
        if local_now >= scheduled_today:
            scheduled_tomorrow = datetime.combine(
                local_today + timedelta(days=1), refresh_time, tzinfo=self._tz
            )
            return scheduled_tomorrow
        return scheduled_today

    async def run(self) -> None:
        """Run the scheduler loop until stopped."""
        while not self._stopped.is_set():
            try:
                await self._wait_and_refresh()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("dodo_token_scheduler_iteration_failed")
                with suppress(TimeoutError):
                    await asyncio.wait_for(self._stopped.wait(), timeout=60)

    def stop(self) -> None:
        """Signal the scheduler to stop after the current iteration."""
        self._stopped.set()

    async def _wait_and_refresh(self) -> None:
        """Wait until the next scheduled time and perform the refresh."""
        now = datetime.now(self._tz)
        next_refresh = self._next_refresh_at(now)
        wait_seconds = (next_refresh - now).total_seconds()

        if wait_seconds > 0:
            logger.info(
                "dodo_token_scheduler_sleeping until=%s seconds=%.1f",
                next_refresh.isoformat(),
                wait_seconds,
            )
            with suppress(TimeoutError):
                await asyncio.wait_for(self._stopped.wait(), timeout=wait_seconds)
                return

        if self._stopped.is_set():
            return

        await self._refresh_with_retry()

    async def _refresh_with_retry(self) -> None:
        """Attempt to refresh with exponential backoff on failure."""
        last_error: Exception | None = None
        delay = INITIAL_RETRY_DELAY_SECONDS

        for attempt in range(MAX_RETRY_ATTEMPTS):
            if self._stopped.is_set():
                return
            try:
                await self.refresher.refresh()
                logger.info("dodo_token_scheduler_refresh_success attempt=%s", attempt + 1)
                return
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                last_error = exc
                logger.warning(
                    "dodo_token_scheduler_refresh_failed attempt=%s error=%s",
                    attempt + 1,
                    str(exc),
                )
                if attempt < MAX_RETRY_ATTEMPTS - 1:
                    with suppress(TimeoutError):
                        await asyncio.wait_for(self._stopped.wait(), timeout=delay)
                    delay = min(delay * 2, MAX_RETRY_DELAY_SECONDS)

        await self._alert_admins(last_error)

    async def _alert_admins(self, error: Exception | None) -> None:
        """Send alert to admin Telegram IDs about persistent refresh failure."""
        if self.bot is None or not self.settings.admin_telegram_ids:
            logger.error(
                "dodo_token_scheduler_all_retries_exhausted error=%s",
                str(error) if error else "unknown",
            )
            return

        message = (
            "Не удалось обновить токен Dodo IS после нескольких попыток.\n\n"
            f"Ошибка: {error}\n\n"
            "Проверьте настройки DODO_OAUTH_* и при необходимости перезапустите бота "
            "с новым DODO_OAUTH_REFRESH_TOKEN."
        )

        for admin_id in self.settings.admin_telegram_ids:
            with suppress(Exception):
                await self.bot.send_message(admin_id, message)
                logger.info("dodo_token_scheduler_admin_alerted admin_id=%s", admin_id)
