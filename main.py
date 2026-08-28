from __future__ import annotations

import asyncio
import logging
from contextlib import suppress

import uvicorn

from app.bot.application import run_polling
from app.config import get_settings

logger = logging.getLogger(__name__)


async def run_combined() -> None:
    """Run both the Telegram bot and FastAPI server in a single process."""
    settings = get_settings()

    # Configure uvicorn server (for OAuth callback and compatibility API)
    config = uvicorn.Config(
        "app.main:app",
        host=settings.app_host,
        port=settings.app_port,
        log_level=settings.log_level.lower(),
    )
    server = uvicorn.Server(config)

    # Disable uvicorn's default signal handlers so we can manage shutdown ourselves
    server.install_signal_handlers = lambda: None

    async def run_bot() -> None:
        try:
            await run_polling(settings)
        except asyncio.CancelledError:
            logger.info("telegram_bot_cancelled")

    async def run_server() -> None:
        try:
            await server.serve()
        except asyncio.CancelledError:
            logger.info("fastapi_server_cancelled")

    logger.info(
        "starting_combined_bot_and_server host=%s port=%s",
        settings.app_host,
        settings.app_port,
    )

    # Run both concurrently; when either stops, cancel the other
    bot_task = asyncio.create_task(run_bot(), name="telegram-bot")
    server_task = asyncio.create_task(run_server(), name="fastapi-server")

    try:
        done, pending = await asyncio.wait(
            [bot_task, server_task],
            return_when=asyncio.FIRST_COMPLETED,
        )
        # If one finished (error or shutdown), cancel the other
        for task in pending:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        # Re-raise any exception from the completed task
        for task in done:
            task.result()
    except KeyboardInterrupt:
        logger.info("shutdown_requested")
        bot_task.cancel()
        server_task.cancel()
        await asyncio.gather(bot_task, server_task, return_exceptions=True)


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    asyncio.run(run_combined())
