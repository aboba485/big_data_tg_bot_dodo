from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from cryptography.fernet import Fernet

from app.config import Settings
from app.dodo.client import DodoApiClient
from app.dodo.oauth import ACCESS_TOKEN_LEEWAY_SECONDS, DodoTokenRefresher
from app.dodo.token_scheduler import DodoTokenRefreshScheduler
from app.errors import ConfigurationError, DodoTokenRefreshError
from app.storage.dodo_tokens import DodoTokenRepository
from app.storage.sqlite import SQLiteDatabase


def _oauth_settings(settings: Settings, **overrides) -> Settings:
    key = Fernet.generate_key().decode()
    values = {
        "dodo_oauth_client_id": "client-id",
        "dodo_oauth_client_secret": "client-secret",
        "dodo_oauth_refresh_token": "seed-refresh-token",
        "dodo_token_encryption_key": key,
        "dodo_token_refresh_hour": 3,
        "dodo_token_refresh_minute": 5,
    }
    values.update(overrides)
    return settings.model_copy(update=values)


def _token_handler(
    *,
    access_token: str = "access-1",
    refresh_token: str = "refresh-1",
    expires_in: int = 3600,
    status: int = 200,
    error: str | None = None,
):
    def handler(request: httpx.Request) -> httpx.Response:
        if error is not None or status >= 400:
            return httpx.Response(status, json={"error": error or "invalid_grant"})
        return httpx.Response(
            200,
            json={
                "access_token": access_token,
                "refresh_token": refresh_token,
                "expires_in": expires_in,
            },
        )

    return handler


def _refresher(
    settings: Settings, handler=None, repository: DodoTokenRepository | None = None
) -> DodoTokenRefresher:
    oauth_settings = _oauth_settings(settings)
    database = SQLiteDatabase(oauth_settings.sqlite_path)
    database.initialize()
    repo = repository or DodoTokenRepository(database, oauth_settings.dodo_token_encryption_key)
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler or _token_handler()))
    return DodoTokenRefresher(oauth_settings, repo, client)


def test_dodo_oauth_disabled_without_full_configuration(settings) -> None:
    assert settings.dodo_oauth_enabled is False
    partial = settings.model_copy(update={"dodo_oauth_client_id": "client-id"})
    assert partial.dodo_oauth_enabled is False
    full = _oauth_settings(settings)
    assert full.dodo_oauth_enabled is True


@pytest.mark.asyncio
async def test_bootstrap_from_seed_token(settings) -> None:
    refresher = _refresher(settings)

    access_token = await refresher.bootstrap()

    assert access_token == "access-1"
    stored = refresher.repository.get()
    assert stored is not None
    assert stored.access_token == "access-1"
    assert stored.refresh_token == "refresh-1"


@pytest.mark.asyncio
async def test_bootstrap_reuses_fresh_stored_token(settings) -> None:
    oauth_settings = _oauth_settings(settings)
    database = SQLiteDatabase(oauth_settings.sqlite_path)
    database.initialize()
    repo = DodoTokenRepository(database, oauth_settings.dodo_token_encryption_key)
    repo.upsert("stored-access", "stored-refresh", datetime.now(UTC) + timedelta(hours=1))
    refresher = DodoTokenRefresher(
        oauth_settings,
        repo,
        httpx.AsyncClient(transport=httpx.MockTransport(_token_handler())),
    )

    access_token = await refresher.bootstrap()

    assert access_token == "stored-access"


@pytest.mark.asyncio
async def test_bootstrap_refreshes_expired_stored_token(settings) -> None:
    oauth_settings = _oauth_settings(settings)
    database = SQLiteDatabase(oauth_settings.sqlite_path)
    database.initialize()
    repo = DodoTokenRepository(database, oauth_settings.dodo_token_encryption_key)
    repo.upsert("expired-access", "stored-refresh", datetime.now(UTC) - timedelta(minutes=5))
    refresher = DodoTokenRefresher(
        oauth_settings,
        repo,
        httpx.AsyncClient(
            transport=httpx.MockTransport(
                _token_handler(access_token="new-access", refresh_token="new-refresh")
            )
        ),
    )

    access_token = await refresher.bootstrap()

    assert access_token == "new-access"
    stored = repo.get()
    assert stored is not None
    assert stored.refresh_token == "new-refresh"


@pytest.mark.asyncio
async def test_refresh_uses_stored_token(settings) -> None:
    oauth_settings = _oauth_settings(settings)
    database = SQLiteDatabase(oauth_settings.sqlite_path)
    database.initialize()
    repo = DodoTokenRepository(database, oauth_settings.dodo_token_encryption_key)
    repo.upsert("old-access", "old-refresh", datetime.now(UTC) - timedelta(minutes=5))
    refresher = DodoTokenRefresher(
        oauth_settings,
        repo,
        httpx.AsyncClient(
            transport=httpx.MockTransport(_token_handler(access_token="refreshed-access"))
        ),
    )

    access_token = await refresher.refresh()

    assert access_token == "refreshed-access"


@pytest.mark.asyncio
async def test_refresh_fails_without_stored_or_seed_token(settings) -> None:
    oauth_settings = _oauth_settings(settings, dodo_oauth_refresh_token="")
    database = SQLiteDatabase(oauth_settings.sqlite_path)
    database.initialize()
    repo = DodoTokenRepository(database, oauth_settings.dodo_token_encryption_key)
    refresher = DodoTokenRefresher(
        oauth_settings,
        repo,
        httpx.AsyncClient(transport=httpx.MockTransport(_token_handler())),
    )

    with pytest.raises(ConfigurationError, match="refresh_token"):
        await refresher.refresh()


@pytest.mark.asyncio
async def test_refresh_raises_on_auth_error(settings) -> None:
    refresher = _refresher(settings, _token_handler(status=401, error="invalid_grant"))

    with pytest.raises(DodoTokenRefreshError, match="отклонил"):
        await refresher.refresh()


@pytest.mark.asyncio
async def test_get_access_token_returns_fresh_token(settings) -> None:
    oauth_settings = _oauth_settings(settings)
    database = SQLiteDatabase(oauth_settings.sqlite_path)
    database.initialize()
    repo = DodoTokenRepository(database, oauth_settings.dodo_token_encryption_key)
    repo.upsert("valid-access", "valid-refresh", datetime.now(UTC) + timedelta(hours=1))
    refresher = DodoTokenRefresher(
        oauth_settings,
        repo,
        httpx.AsyncClient(transport=httpx.MockTransport(_token_handler())),
    )

    token = await refresher.get_access_token()

    assert token == "valid-access"


@pytest.mark.asyncio
async def test_get_access_token_refreshes_when_near_expiry(settings) -> None:
    oauth_settings = _oauth_settings(settings)
    database = SQLiteDatabase(oauth_settings.sqlite_path)
    database.initialize()
    repo = DodoTokenRepository(database, oauth_settings.dodo_token_encryption_key)
    expires_soon = datetime.now(UTC) + timedelta(seconds=ACCESS_TOKEN_LEEWAY_SECONDS - 1)
    repo.upsert("expiring-access", "valid-refresh", expires_soon)
    refresher = DodoTokenRefresher(
        oauth_settings,
        repo,
        httpx.AsyncClient(
            transport=httpx.MockTransport(_token_handler(access_token="fresh-access"))
        ),
    )

    token = await refresher.get_access_token()

    assert token == "fresh-access"


@pytest.mark.asyncio
async def test_dodo_api_client_with_token_provider(settings) -> None:
    async def token_provider() -> str:
        return "provided-token"

    client = DodoApiClient(
        httpx.AsyncClient(),
        country_id="ru",
        allowed_operations=set(),
        token_provider=token_provider,
    )

    token = await client._get_access_token()

    assert token == "provided-token"


@pytest.mark.asyncio
async def test_dodo_api_client_fallback_to_static_token(settings) -> None:
    client = DodoApiClient(
        httpx.AsyncClient(),
        access_token="static-token",
        country_id="ru",
        allowed_operations=set(),
    )

    token = await client._get_access_token()

    assert token == "static-token"


@pytest.mark.asyncio
async def test_dodo_api_client_raises_without_any_token(settings) -> None:
    client = DodoApiClient(
        httpx.AsyncClient(),
        country_id="ru",
        allowed_operations=set(),
    )

    with pytest.raises(ConfigurationError, match="DODO_ACCESS_TOKEN"):
        await client._get_access_token()


def test_scheduler_computes_next_refresh_time(settings) -> None:
    oauth_settings = _oauth_settings(
        settings, dodo_token_refresh_hour=3, dodo_token_refresh_minute=5
    )
    refresher = _refresher(oauth_settings)
    scheduler = DodoTokenRefreshScheduler(oauth_settings, refresher)

    from zoneinfo import ZoneInfo

    tz = ZoneInfo(oauth_settings.app_timezone)

    before = datetime(2026, 8, 14, 2, 0, tzinfo=tz)
    next_run = scheduler._next_refresh_at(before)
    assert next_run.hour == 3
    assert next_run.minute == 5
    assert next_run.date() == before.date()

    after = datetime(2026, 8, 14, 4, 0, tzinfo=tz)
    next_run = scheduler._next_refresh_at(after)
    assert next_run.hour == 3
    assert next_run.minute == 5
    assert next_run.date() == (after.date() + timedelta(days=1))


@pytest.mark.asyncio
async def test_scheduler_refresh_with_retry(settings) -> None:
    oauth_settings = _oauth_settings(settings)
    database = SQLiteDatabase(oauth_settings.sqlite_path)
    database.initialize()
    repo = DodoTokenRepository(database, oauth_settings.dodo_token_encryption_key)
    repo.upsert("old-access", "old-refresh", datetime.now(UTC) - timedelta(hours=1))

    call_count = 0

    def failing_then_success_handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        if call_count < 3:
            return httpx.Response(500, json={"error": "server_error"})
        return httpx.Response(
            200,
            json={
                "access_token": "success-access",
                "refresh_token": "new-refresh",
                "expires_in": 3600,
            },
        )

    refresher = DodoTokenRefresher(
        oauth_settings,
        repo,
        httpx.AsyncClient(transport=httpx.MockTransport(failing_then_success_handler)),
    )
    scheduler = DodoTokenRefreshScheduler(oauth_settings, refresher)

    with patch.object(asyncio, "wait_for", new_callable=AsyncMock) as mock_wait:
        mock_wait.side_effect = TimeoutError

        await scheduler._refresh_with_retry()

    assert call_count == 3
    stored = repo.get()
    assert stored is not None
    assert stored.access_token == "success-access"


@pytest.mark.asyncio
async def test_scheduler_alerts_admins_after_max_retries(settings) -> None:
    oauth_settings = _oauth_settings(settings, admin_telegram_ids=[123, 456])
    database = SQLiteDatabase(oauth_settings.sqlite_path)
    database.initialize()
    repo = DodoTokenRepository(database, oauth_settings.dodo_token_encryption_key)
    repo.upsert("old-access", "old-refresh", datetime.now(UTC) - timedelta(hours=1))

    refresher = DodoTokenRefresher(
        oauth_settings,
        repo,
        httpx.AsyncClient(
            transport=httpx.MockTransport(_token_handler(status=500, error="server_error"))
        ),
    )
    bot = AsyncMock()
    scheduler = DodoTokenRefreshScheduler(oauth_settings, refresher, bot)

    with patch.object(asyncio, "wait_for", new_callable=AsyncMock) as mock_wait:
        mock_wait.side_effect = TimeoutError

        await scheduler._refresh_with_retry()

    assert bot.send_message.await_count == 2
    assert bot.send_message.await_args_list[0].args[0] == 123
    assert bot.send_message.await_args_list[1].args[0] == 456
