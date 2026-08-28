from __future__ import annotations

import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from app.bot.application import (
    create_dispatcher,
    global_error_handler,
    run_polling,
    validate_runtime_settings,
)
from app.bot.middlewares.access import AccessMiddleware
from app.bot.models import BotPreparation, BotReportResult
from app.bot.services import BotReportService, PreparationContext
from app.errors import ConfigurationError
from app.planner.schemas import OutputFormat, PlannerResult, PlanStatus, ReportPlan
from app.reports.metric_registry import MetricRegistry
from app.services import build_services
from app.storage.sqlite import SQLiteDatabase
from app.storage.users import TelegramUserRepository
from app.users.models import TelegramUser
from app.users.service import UserAccessService


@pytest.mark.asyncio
async def test_unknown_user_is_denied_with_telegram_id(settings) -> None:
    database = SQLiteDatabase(settings.sqlite_path)
    database.initialize()
    access = UserAccessService(TelegramUserRepository(database))
    middleware = AccessMiddleware(settings, access)
    middleware._answer = AsyncMock()  # type: ignore[method-assign]
    handler = AsyncMock()
    event = SimpleNamespace(
        from_user=SimpleNamespace(id=123456789),
        chat=SimpleNamespace(type="private"),
    )

    await middleware(handler, event, {})

    handler.assert_not_awaited()
    middleware._answer.assert_awaited_once()
    assert "123456789" in middleware._answer.await_args.args[1]


@pytest.mark.asyncio
async def test_public_user_reaches_handler_without_registration(settings) -> None:
    database = SQLiteDatabase(settings.sqlite_path)
    database.initialize()
    repository = TelegramUserRepository(database)
    access = UserAccessService(
        repository,
        public_access=True,
        public_report_types=["sales"],
        public_unit_ids=["unit-public"],
    )
    middleware = AccessMiddleware(settings, access)
    handler = AsyncMock()
    event = SimpleNamespace(
        from_user=SimpleNamespace(id=987654321),
        chat=SimpleNamespace(type="private"),
        text="Покажи выручку",
    )
    data = {}

    await middleware(handler, event, data)

    handler.assert_awaited_once()
    assert data["telegram_user"].telegram_id == 987654321
    assert repository.get(987654321) is None


def test_rate_limit_is_per_user(settings) -> None:
    database = SQLiteDatabase(settings.sqlite_path)
    database.initialize()
    access = UserAccessService(TelegramUserRepository(database))
    middleware = AccessMiddleware(
        settings.model_copy(update={"telegram_rate_limit_per_minute": 2}),
        access,
    )

    assert middleware._allow_request(1) is True
    assert middleware._allow_request(1) is True
    assert middleware._allow_request(1) is False
    assert middleware._allow_request(2) is True


@pytest.mark.asyncio
async def test_internal_database_error_is_hidden_from_user() -> None:
    message = SimpleNamespace(
        from_user=SimpleNamespace(id=42),
        answer=AsyncMock(),
    )
    update = SimpleNamespace(message=message, callback_query=None)
    event = SimpleNamespace(
        exception=sqlite3.OperationalError("secret database path"),
        update=update,
    )

    assert await global_error_handler(event) is True
    public_text = message.answer.await_args.args[0]
    assert "RPT-" in public_text
    assert "secret database path" not in public_text


@pytest.mark.asyncio
async def test_dispatcher_builds_without_telegram_network(settings) -> None:
    async with httpx.AsyncClient() as client:
        services = build_services(settings, client)
        dispatcher = create_dispatcher(settings, services)

        assert dispatcher["bot_report_service"] is not None
        assert set(dispatcher.resolve_used_update_types()) == {"callback_query", "message"}


@pytest.mark.asyncio
async def test_polling_requires_bot_token_before_network(settings) -> None:
    with pytest.raises(ConfigurationError, match="TELEGRAM_BOT_TOKEN"):
        await run_polling(settings.model_copy(update={"telegram_bot_token": ""}))


@pytest.mark.parametrize(
    ("updates", "missing_name"),
    [
        (
            {
                "telegram_bot_token": "123456:TEST",
                "planner_mock_mode": False,
                "openai_api_key": "",
            },
            "OPENAI_API_KEY",
        ),
        (
            {
                "telegram_bot_token": "123456:TEST",
                "dodo_mock_mode": False,
                "dodo_access_token": "",
            },
            "DODO_ACCESS_TOKEN",
        ),
    ],
)
def test_runtime_settings_fail_fast_for_real_services(settings, updates, missing_name) -> None:
    with pytest.raises(ConfigurationError, match=missing_name):
        validate_runtime_settings(settings.model_copy(update=updates))


@pytest.mark.asyncio
async def test_large_text_report_is_exported_as_csv(settings, tmp_path) -> None:
    class FakeFiles:
        def __init__(self) -> None:
            self.value = None

        def add(self, report_id, request_id, path, media_type) -> None:
            self.value = (report_id, request_id, path, media_type)

        def get(self, report_id):
            if self.value and self.value[0] == report_id:
                return self.value[2], self.value[3]
            return None

        def delete(self, _report_id) -> None:
            return None

    response = {
        "request_id": "request-1",
        "status": "ready",
        "summary": "Большой отчёт",
        "columns": ["value"],
        "rows": [{"value": index} for index in range(3)],
        "totals": {},
        "download": None,
    }
    files = FakeFiles()
    user = TelegramUser(
        telegram_id=1,
        allowed_report_types=["*"],
        allowed_unit_ids=["*"],
    )
    minimal = SimpleNamespace(
        authorized=lambda telegram_id: user if telegram_id == user.telegram_id else None,
        ensure_report_access=lambda _user, _reports: None,
        ensure_unit_access=lambda _user, _units: None,
    )
    service = BotReportService(
        settings.model_copy(update={"reports_directory": tmp_path, "telegram_max_message_rows": 2}),
        orchestrator=minimal,
        retrieval=minimal,
        planner=minimal,
        validator=minimal,
        metrics=MetricRegistry(),
        resolver=minimal,
        files=files,
        user_access=minimal,
    )
    plan = ReportPlan(status=PlanStatus.READY)
    service._prepare_context = AsyncMock(  # type: ignore[method-assign]
        return_value=PreparationContext(
            public=BotPreparation(status="ready"),
            plan=plan,
            planner_result=PlannerResult(plan=plan),
        )
    )
    service.orchestrator.create_prepared_report = AsyncMock(return_value=response)

    result = await service.run(
        user,
        "large report query",
        OutputFormat.TABLE,
    )

    assert isinstance(result, BotReportResult)
    assert result.file_path is not None and result.file_path.suffix == ".csv"


@pytest.mark.asyncio
async def test_wide_text_report_is_not_silently_truncated(settings, tmp_path) -> None:
    class FakeFiles:
        def __init__(self) -> None:
            self.value = None

        def add(self, report_id, request_id, path, media_type) -> None:
            self.value = (report_id, request_id, path, media_type)

        def get(self, report_id):
            if self.value and self.value[0] == report_id:
                return self.value[2], self.value[3]
            return None

        def delete(self, _report_id) -> None:
            return None

    user = TelegramUser(
        telegram_id=1,
        allowed_report_types=["*"],
        allowed_unit_ids=["*"],
    )
    access = SimpleNamespace(
        authorized=lambda telegram_id: user if telegram_id == user.telegram_id else None,
        ensure_report_access=lambda _user, _reports: None,
        ensure_unit_access=lambda _user, _units: None,
    )
    minimal = SimpleNamespace()
    files = FakeFiles()
    service = BotReportService(
        settings.model_copy(update={"reports_directory": tmp_path}),
        orchestrator=minimal,
        retrieval=minimal,
        planner=minimal,
        validator=minimal,
        metrics=MetricRegistry(),
        resolver=minimal,
        files=files,
        user_access=access,
    )
    plan = ReportPlan(status=PlanStatus.READY)
    service._prepare_context = AsyncMock(  # type: ignore[method-assign]
        return_value=PreparationContext(
            public=BotPreparation(status="ready"),
            plan=plan,
            planner_result=PlannerResult(plan=plan),
        )
    )
    service.orchestrator.create_prepared_report = AsyncMock(
        return_value={
            "request_id": "request-wide",
            "status": "ready",
            "summary": "Большой отчёт",
            "columns": ["value"],
            "rows": [{"value": "x" * 5000}],
            "totals": {},
            "download": None,
        }
    )

    result = await service.run(user, "wide report query", OutputFormat.TABLE)

    assert result.file_path is not None and result.file_path.suffix == ".csv"
    assert result.text is not None and "Полный результат приложен файлом" in result.text
