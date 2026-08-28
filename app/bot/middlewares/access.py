from __future__ import annotations

import logging
import time
from collections import defaultdict, deque
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject

from app.bot.messages.texts import ACCESS_DENIED
from app.config import Settings
from app.users.service import UserAccessService

logger = logging.getLogger(__name__)


class AccessMiddleware(BaseMiddleware):
    def __init__(self, settings: Settings, access: UserAccessService) -> None:
        self.settings = settings
        self.access = access
        self._requests: defaultdict[int, deque[float]] = defaultdict(deque)

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        from_user = getattr(event, "from_user", None)
        if from_user is None:
            return None
        if self.settings.telegram_private_chats_only:
            chat = getattr(event, "chat", None)
            if isinstance(event, CallbackQuery) and event.message:
                chat = event.message.chat
            if chat is not None and chat.type != "private":
                await self._answer(event, "Бот работает только в личных сообщениях.")
                return None
        user = self.access.authorized(from_user.id)
        if user is None:
            await self._answer(event, ACCESS_DENIED.format(telegram_id=from_user.id))
            return None
        if not self._allow_request(from_user.id):
            await self._answer(event, "Слишком много запросов. Повторите попытку через минуту.")
            return None
        action = "message"
        if isinstance(event, Message) and (event.text or "").startswith("/"):
            action = f"command:{(event.text or '').split()[0][:32]}"
        elif isinstance(event, CallbackQuery):
            action = f"callback:{(event.data or '')[:48]}"
        logger.info("bot_action telegram_id=%s action=%s", from_user.id, action)
        data["telegram_user"] = user
        return await handler(event, data)

    def _allow_request(self, telegram_id: int) -> bool:
        now = time.monotonic()
        values = self._requests[telegram_id]
        while values and now - values[0] >= 60:
            values.popleft()
        if len(values) >= self.settings.telegram_rate_limit_per_minute:
            return False
        values.append(now)
        return True

    @staticmethod
    async def _answer(event: TelegramObject, text: str) -> None:
        if isinstance(event, Message):
            await event.answer(text)
        elif isinstance(event, CallbackQuery):
            await event.answer(text, show_alert=True)
