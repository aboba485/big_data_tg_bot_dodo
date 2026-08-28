from __future__ import annotations

import asyncio
import logging
import secrets
from typing import Any

import httpx
from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import ErrorEvent

from app.bot.errors import BotAccessError, BotBusyError, BotInputError
from app.bot.handlers import (
    common_router,
    google_drive_router,
    reports_router,
    weekly_router,
)
from app.bot.messages.texts import GENERIC_ERROR
from app.bot.middlewares import AccessMiddleware
from app.bot.scheduler import WeeklyReportScheduler
from app.bot.services import BotReportService
from app.config import Settings, get_settings
from app.dodo.token_scheduler import DodoTokenRefreshScheduler
from app.errors import ApplicationError, ConfigurationError
from app.services import build_services

logger = logging.getLogger(__name__)


def validate_runtime_settings(settings: Settings) -> None:
    if not settings.telegram_bot_token:
        raise ConfigurationError("Не задан TELEGRAM_BOT_TOKEN")
    if not settings.planner_mock_mode and not settings.openai_api_key:
        raise ConfigurationError("Не задан OPENAI_API_KEY")
    has_dodo_auth = settings.dodo_access_token or settings.dodo_oauth_enabled
    if not settings.dodo_mock_mode and not has_dodo_auth:
        raise ConfigurationError("Не задан DODO_ACCESS_TOKEN и не настроен Dodo OAuth")


def create_dispatcher(settings: Settings, services: dict[str, Any]) -> Dispatcher:
    dispatcher = Dispatcher(storage=MemoryStorage())
    access_middleware = AccessMiddleware(settings, services["user_access"])
    dispatcher.message.outer_middleware(access_middleware)
    dispatcher.callback_query.outer_middleware(access_middleware)
    dispatcher.include_router(common_router)
    # Registered before the FSM routers so /drive is never consumed as a report query.
    dispatcher.include_router(google_drive_router)
    dispatcher.include_router(weekly_router)
    dispatcher.include_router(reports_router)
    report_service = BotReportService(
        settings=settings,
        orchestrator=services["orchestrator"],
        retrieval=services["retrieval"],
        planner=services["planner"],
        validator=services["validator"],
        metrics=services["metrics"],
        resolver=services["resolver"],
        files=services["files"],
        user_access=services["user_access"],
        google_drive=services["google_drive"],
    )
    dispatcher["bot_report_service"] = report_service
    dispatcher["weekly_reports"] = services["weekly_reports"]
    dispatcher["google_drive"] = services["google_drive"]
    dispatcher.errors.register(global_error_handler)
    return dispatcher


async def global_error_handler(event: ErrorEvent) -> bool:
    error = event.exception
    update = event.update
    message = update.message
    callback = update.callback_query
    if message is None and callback is not None:
        message = callback.message
    from_user = callback.from_user if callback else (message.from_user if message else None)
    telegram_id = from_user.id if from_user else None
    if isinstance(error, (BotInputError, BotAccessError, BotBusyError)):
        text = str(error)
        logger.info(
            "bot_request_rejected telegram_id=%s error_type=%s",
            telegram_id,
            type(error).__name__,
        )
    else:
        error_code = f"RPT-{secrets.token_hex(3).upper()}"
        text = GENERIC_ERROR.format(error_code=error_code)
        logger.exception(
            "bot_unhandled_error telegram_id=%s error_code=%s application_code=%s",
            telegram_id,
            error_code,
            error.code if isinstance(error, ApplicationError) else None,
        )
    if callback and callback.message:
        await callback.message.answer(text)
    elif message:
        await message.answer(text)
    return True


async def run_polling(settings: Settings | None = None) -> None:
    current_settings = settings or get_settings()
    validate_runtime_settings(current_settings)
    logging.basicConfig(
        level=current_settings.log_level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    logger.info("telegram_bot_starting")
    timeout = httpx.Timeout(current_settings.dodo_request_timeout_seconds)
    transport = (
        httpx.MockTransport(lambda _request: httpx.Response(500))
        if current_settings.dodo_mock_mode
        else None
    )
    async with httpx.AsyncClient(timeout=timeout, transport=transport) as http_client:
        services = build_services(current_settings, http_client)
        dispatcher = create_dispatcher(current_settings, services)
        bot = Bot(token=current_settings.telegram_bot_token)

        dodo_token_refresher = services.get("dodo_token_refresher")
        if dodo_token_refresher is not None:
            logger.info("dodo_oauth_bootstrap_starting")
            await dodo_token_refresher.bootstrap()
            logger.info("dodo_oauth_bootstrap_complete")

        weekly_scheduler = WeeklyReportScheduler(
            bot,
            services["weekly_reports"],
            dispatcher["bot_report_service"],
            current_settings.telegram_scheduler_poll_seconds,
        )
        weekly_task = asyncio.create_task(weekly_scheduler.run(), name="weekly-report-scheduler")

        dodo_token_scheduler: DodoTokenRefreshScheduler | None = None
        dodo_token_task: asyncio.Task[None] | None = None
        if dodo_token_refresher is not None:
            dodo_token_scheduler = DodoTokenRefreshScheduler(
                current_settings, dodo_token_refresher, bot
            )
            dodo_token_task = asyncio.create_task(
                dodo_token_scheduler.run(), name="dodo-token-scheduler"
            )
            logger.info("dodo_token_scheduler_started")

        try:
            await dispatcher.start_polling(
                bot, allowed_updates=dispatcher.resolve_used_update_types()
            )
        finally:
            weekly_scheduler.stop()
            weekly_task.cancel()
            await asyncio.gather(weekly_task, return_exceptions=True)

            if dodo_token_scheduler is not None and dodo_token_task is not None:
                dodo_token_scheduler.stop()
                dodo_token_task.cancel()
                await asyncio.gather(dodo_token_task, return_exceptions=True)
                logger.info("dodo_token_scheduler_stopped")

            await services["google_drive"].aclose()
            await bot.session.close()
            logger.info("telegram_bot_stopped")
