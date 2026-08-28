from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, unquote, urlparse

import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from app.bot.errors import BotInputError
from app.bot.handlers.google_drive import (
    drive_link_callback,
    drive_show_callback,
    drive_unlink_callback,
)
from app.bot.handlers.reports import _parse_format
from app.bot.keyboards import allowed_formats, format_keyboard
from app.bot.scheduler import WeeklyReportScheduler
from app.bot.services import BotReportService
from app.config import Settings
from app.errors import GoogleDriveError, GoogleDriveNotLinkedError
from app.google_drive.service import (
    DRIVE_FILE_SCOPE,
    DRIVE_FILES_ENDPOINT,
    SHEETS_ENDPOINT,
    TOKEN_ENDPOINT,
    USERINFO_ENDPOINT,
    GoogleDriveService,
    generate_encryption_key,
)
from app.main import create_app
from app.planner.schemas import OutputFormat
from app.services import build_services
from app.storage.google_drive import GoogleDriveLinkRepository, OAuthStateRepository
from app.storage.sqlite import SQLiteDatabase
from app.storage.weekly_reports import WeeklyReportRepository
from app.users.models import TelegramRole, TelegramUser
from tests.conftest import UNIT_ID

REDIRECT_URI = "https://reports.example.com/google/oauth/callback"
SPREADSHEET_URL = "https://docs.google.com/spreadsheets/d/sheet-1"


def _drive_settings(settings: Settings, **overrides) -> Settings:
    values = {
        "google_client_id": "client-id",
        "google_client_secret": "client-secret",
        "google_redirect_uri": REDIRECT_URI,
        "google_token_encryption_key": generate_encryption_key(),
    }
    values.update(overrides)
    return settings.model_copy(update=values)


def _google_handler(
    requests: list[httpx.Request],
    *,
    token_payload: dict | None = None,
    token_status: int = 200,
    sheets_status: int = 200,
    sheets_error: dict | None = None,
    existing_values: list[list] | None = None,
    values_status: int = 200,
):
    default_token = {
        "access_token": "access-1",
        "refresh_token": "refresh-1",
        "expires_in": 3600,
        "scope": f"openid email {DRIVE_FILE_SCOPE}",
    }

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        url = str(request.url)
        if url.startswith(TOKEN_ENDPOINT):
            payload = default_token if token_payload is None else token_payload
            return httpx.Response(token_status, json=payload)
        if url.startswith(USERINFO_ENDPOINT):
            return httpx.Response(200, json={"email": "user@example.com"})
        if request.method == "GET" and "/values/" in url:
            if values_status >= 400:
                return httpx.Response(values_status, json={"error": {"status": "NOT_FOUND"}})
            return httpx.Response(200, json={"values": existing_values or []})
        if request.method == "POST" and url.startswith(SHEETS_ENDPOINT):
            if sheets_status >= 400:
                return httpx.Response(
                    sheets_status,
                    json=sheets_error or {"error": {"status": "PERMISSION_DENIED"}},
                )
            return httpx.Response(
                200, json={"spreadsheetId": "sheet-1", "spreadsheetUrl": SPREADSHEET_URL}
            )
        if request.method == "PUT":
            return httpx.Response(200, json={"updatedCells": 1})
        return httpx.Response(404, json={"error": "unexpected_call"})

    return handler


def _service(settings: Settings, handler) -> GoogleDriveService:
    database = SQLiteDatabase(settings.sqlite_path)
    database.initialize()
    return GoogleDriveService(
        settings,
        GoogleDriveLinkRepository(database),
        OAuthStateRepository(database, ttl_seconds=settings.google_oauth_state_ttl_seconds),
        httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )


async def _link(service: GoogleDriveService, telegram_id: int = 1) -> tuple[int, str]:
    url = service.create_auth_url(telegram_id)
    state = parse_qs(urlparse(url).query)["state"][0]
    return await service.complete_link("auth-code", state)


def _admin(telegram_id: int = 1) -> TelegramUser:
    return TelegramUser(
        telegram_id=telegram_id,
        role=TelegramRole.ADMIN,
        allowed_report_types=["*"],
        allowed_unit_ids=["*"],
    )


class FakeMessage:
    def __init__(self) -> None:
        self.answers: list[tuple[str, object | None]] = []

    async def answer(self, text: str, reply_markup=None) -> None:
        self.answers.append((text, reply_markup))


class FakeCallback:
    def __init__(self, data: str) -> None:
        self.data = data
        self.message = FakeMessage()
        self.answered = False

    async def answer(self, text: str | None = None, show_alert: bool = False) -> None:
        self.answered = True


class FakeState:
    def __init__(self) -> None:
        self.cleared = False

    async def clear(self) -> None:
        self.cleared = True


def test_disabled_without_full_configuration(settings) -> None:
    assert settings.google_drive_enabled is False
    partial = settings.model_copy(update={"google_client_id": "client-id"})

    assert partial.google_drive_enabled is False
    assert _drive_settings(settings).google_drive_enabled is True


def test_plain_http_redirect_uri_is_rejected(settings) -> None:
    with pytest.raises(ValueError, match="HTTPS"):
        Settings(_env_file=None, google_redirect_uri="http://reports.example.com/callback")

    localhost = Settings(_env_file=None, google_redirect_uri="http://localhost:8000/callback")
    assert localhost.google_redirect_uri.endswith("/callback")


def test_auth_url_requests_offline_access_and_minimal_scope(settings) -> None:
    service = _service(_drive_settings(settings), _google_handler([]))

    url = service.create_auth_url(7)
    query = parse_qs(urlparse(url).query)

    assert query["access_type"] == ["offline"]
    assert query["prompt"] == ["consent"]
    assert query["redirect_uri"] == [REDIRECT_URI]
    assert "https://www.googleapis.com/auth/drive.file" in query["scope"][0]
    assert "https://www.googleapis.com/auth/drive" not in query["scope"][0].split()
    assert len(query["state"][0]) >= 32


@pytest.mark.asyncio
async def test_link_stores_encrypted_tokens_only(settings) -> None:
    drive_settings = _drive_settings(settings)
    service = _service(drive_settings, _google_handler([]))

    telegram_id, email = await _link(service, 5)

    assert (telegram_id, email) == (5, "user@example.com")
    assert service.is_linked(5) is True
    assert service.linked_email(5) == "user@example.com"
    stored = service.links.get(5)
    assert stored is not None
    assert "refresh-1" not in stored.encrypted_tokens
    assert "access-1" not in stored.encrypted_tokens
    assert service._decrypt(stored.encrypted_tokens)["refresh_token"] == "refresh-1"


@pytest.mark.asyncio
async def test_state_is_single_use_and_expires(settings) -> None:
    drive_settings = _drive_settings(settings)
    service = _service(drive_settings, _google_handler([]))
    url = service.create_auth_url(1)
    state = parse_qs(urlparse(url).query)["state"][0]

    await service.complete_link("code", state)
    with pytest.raises(GoogleDriveError, match="недействительна"):
        await service.complete_link("code", state)

    expired = service.create_auth_url(1)
    expired_state = parse_qs(urlparse(expired).query)["state"][0]
    stale = datetime.now(UTC) - timedelta(seconds=drive_settings.google_oauth_state_ttl_seconds + 5)
    service.states.create(expired_state, 1, now_utc=stale)
    with pytest.raises(GoogleDriveError, match="недействительна"):
        await service.complete_link("code", expired_state)


@pytest.mark.asyncio
async def test_unknown_state_is_rejected(settings) -> None:
    service = _service(_drive_settings(settings), _google_handler([]))

    with pytest.raises(GoogleDriveError, match="недействительна"):
        await service.complete_link("code", "forged-state")


@pytest.mark.asyncio
async def test_link_requires_refresh_token(settings) -> None:
    service = _service(
        _drive_settings(settings),
        _google_handler([], token_payload={"access_token": "access-1", "expires_in": 3600}),
    )

    with pytest.raises(GoogleDriveError, match="токен обновления"):
        await _link(service)
    assert service.is_linked(1) is False


@pytest.mark.asyncio
async def test_link_requires_the_drive_scope(settings) -> None:
    service = _service(
        _drive_settings(settings),
        _google_handler(
            [],
            token_payload={
                "access_token": "access-1",
                "refresh_token": "refresh-1",
                "expires_in": 3600,
                "scope": "openid email",
            },
        ),
    )

    with pytest.raises(GoogleDriveError, match="Google Диске"):
        await _link(service)
    assert service.is_linked(1) is False


def test_google_calls_do_not_reuse_the_dodo_client(settings) -> None:
    # In mock mode the Dodo client answers every request from a local mock transport.
    dodo_client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _request: httpx.Response(500))
    )
    services = build_services(_drive_settings(settings), dodo_client)

    assert services["google_drive"].http_client is None
    assert services["google_drive"]._client() is not dodo_client


@pytest.mark.asyncio
async def test_spreadsheet_contains_header_rows_and_totals(settings) -> None:
    requests: list[httpx.Request] = []
    service = _service(_drive_settings(settings), _google_handler(requests))
    await _link(service)

    url, spreadsheet_id = await service.create_spreadsheet(
        1,
        title="Выручка",
        columns=["unitName", "sales"],
        rows=[
            {"unitName": "Ресторан 1", "sales": 100.5},
            {"unitName": "Ресторан 2", "sales": None},
        ],
        totals={"sales": 100.5},
    )

    assert url == SPREADSHEET_URL
    assert spreadsheet_id == "sheet-1"
    create = next(
        request
        for request in requests
        if request.method == "POST" and str(request.url).startswith(SHEETS_ENDPOINT)
    )
    body = json.loads(create.content)
    assert body["properties"]["title"] == "Выручка"
    assert body["sheets"][0]["properties"]["gridProperties"] == {"rowCount": 4, "columnCount": 2}

    values = json.loads(next(r for r in requests if r.method == "PUT").content)["values"]
    assert values[0] == ["", "sales"]
    assert values[1] == ["Ресторан 1", 100.5]
    assert values[2] == ["Ресторан 2", ""]
    assert values[3] == ["Итого", 100.5]


@pytest.mark.asyncio
async def test_spreadsheet_pivots_units_across_dates(settings) -> None:
    requests: list[httpx.Request] = []
    service = _service(_drive_settings(settings), _google_handler(requests))
    await _link(service)

    await service.create_spreadsheet(
        1,
        title="Выручка",
        columns=["day", "unitId", "unitName", "sales"],
        rows=[
            {"day": "2026-08-04", "unitId": "1", "unitName": "См1", "sales": 10},
            {"day": "2026-08-11", "unitId": "1", "unitName": "См1", "sales": 20},
            {"day": "2026-08-04", "unitId": "2", "unitName": "К1", "sales": 5},
        ],
        totals={"sales": 35},
    )

    values = json.loads(next(r for r in requests if r.method == "PUT").content)["values"]
    assert values[0] == ["", "04.08.2026", "11.08.2026"]
    assert values[1] == ["См1", 10, 20]
    assert values[2] == ["К1", 5, ""]
    assert values[3] == ["Итого", 15, 20]


@pytest.mark.asyncio
async def test_spreadsheet_uses_period_end_as_column(settings) -> None:
    requests: list[httpx.Request] = []
    service = _service(_drive_settings(settings), _google_handler(requests))
    await _link(service)

    await service.create_spreadsheet(
        1,
        title="Выручка",
        columns=["unitName", "sales"],
        rows=[{"unitName": "См1", "sales": 100.5}],
        totals={"sales": 100.5},
        period_label="2026-08-04 — 2026-08-10",
    )

    values = json.loads(next(r for r in requests if r.method == "PUT").content)["values"]
    assert values[0] == ["", "10.08.2026"]
    assert values[1] == ["См1", 100.5]
    assert values[2] == ["Итого", 100.5]


@pytest.mark.asyncio
async def test_append_adds_a_date_column(settings) -> None:
    requests: list[httpx.Request] = []
    existing = [
        ["", "10.08.2026"],
        ["См1", 100],
        ["К1", 50],
        ["Итого", 150],
    ]
    service = _service(
        _drive_settings(settings),
        _google_handler(requests, existing_values=existing),
    )
    await _link(service)

    url = await service.append_to_spreadsheet(
        1,
        "sheet-1",
        period_label="2026-08-11 — 2026-08-17",
        columns=["unitName", "sales"],
        rows=[{"unitName": "См1", "sales": 80}, {"unitName": "Обн", "sales": 12}],
        totals={"sales": 92},
    )

    assert url == SPREADSHEET_URL
    put = next(request for request in requests if request.method == "PUT")
    values = json.loads(put.content)["values"]
    assert values[0] == ["", "10.08.2026", "17.08.2026"]
    assert values[1] == ["См1", 100, 80]
    assert values[2] == ["К1", 50, ""]
    assert values[3] == ["Обн", "", 12]
    assert values[4] == ["Итого", 150, 92]


@pytest.mark.asyncio
async def test_append_rejects_legacy_row_layout(settings) -> None:
    service = _service(
        _drive_settings(settings),
        _google_handler(
            [],
            existing_values=[["unitName", "sales"], ["Ресторан 1", 100], ["Итого", 100]],
        ),
    )
    await _link(service)

    with pytest.raises(GoogleDriveError, match="другой формат"):
        await service.append_to_spreadsheet(
            1,
            "sheet-1",
            period_label="2026-08-11 — 2026-08-17",
            columns=["unitName", "sales"],
            rows=[{"unitName": "См1", "sales": 80}],
            totals={"sales": 80},
        )


@pytest.mark.asyncio
async def test_large_reports_are_written_in_chunks(settings) -> None:
    requests: list[httpx.Request] = []
    service = _service(_drive_settings(settings), _google_handler(requests))
    await _link(service)

    await service.create_spreadsheet(
        1,
        title="Большой отчёт",
        columns=["day"],
        rows=[{"day": f"2026-06-{index:02d}"} for index in range(1, 11)] * 300,
        totals={},
    )

    puts = [request for request in requests if request.method == "PUT"]
    assert len(puts) == 2

    def written_cell(request: httpx.Request) -> str:
        path = unquote(str(request.url).split("/values/")[1].split("?")[0])
        return path.rsplit("!", 1)[-1]

    assert written_cell(puts[0]) == "A1"
    assert written_cell(puts[1]) == "A2001"


@pytest.mark.asyncio
async def test_row_limit_points_to_file_formats(settings) -> None:
    service = _service(_drive_settings(settings, google_sheets_max_rows=5), _google_handler([]))
    await _link(service)

    with pytest.raises(GoogleDriveError, match="CSV или XLSX"):
        await service.create_spreadsheet(
            1, title="Отчёт", columns=["day"], rows=[{"day": str(i)} for i in range(6)], totals={}
        )


@pytest.mark.asyncio
async def test_spreadsheet_requires_a_linked_account(settings) -> None:
    service = _service(_drive_settings(settings), _google_handler([]))

    with pytest.raises(GoogleDriveNotLinkedError):
        await service.create_spreadsheet(
            1, title="Отчёт", columns=["day"], rows=[{"day": "1"}], totals={}
        )


@pytest.mark.asyncio
async def test_expired_access_token_is_refreshed(settings) -> None:
    requests: list[httpx.Request] = []
    service = _service(_drive_settings(settings), _google_handler(requests))
    await _link(service)
    link = service.links.get(1)
    assert link is not None
    payload = service._decrypt(link.encrypted_tokens)
    payload["expires_at"] = (datetime.now(UTC) - timedelta(minutes=5)).isoformat()
    service.links.upsert(1, link.email, service._encrypt(payload))
    requests.clear()

    await service.create_spreadsheet(
        1, title="Отчёт", columns=["day"], rows=[{"day": "1"}], totals={}
    )

    refresh = next(request for request in requests if str(request.url).startswith(TOKEN_ENDPOINT))
    assert parse_qs(refresh.content.decode())["grant_type"] == ["refresh_token"]


@pytest.mark.asyncio
async def test_revoked_access_removes_the_stored_link(settings) -> None:
    service = _service(_drive_settings(settings), _google_handler([]))
    await _link(service)
    link = service.links.get(1)
    assert link is not None
    payload = service._decrypt(link.encrypted_tokens)
    payload["expires_at"] = (datetime.now(UTC) - timedelta(minutes=5)).isoformat()
    service.links.upsert(1, link.email, service._encrypt(payload))
    service.http_client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            _google_handler([], token_status=400, token_payload={"error": "invalid_grant"})
        )
    )

    with pytest.raises(GoogleDriveNotLinkedError, match="отозван"):
        await service.create_spreadsheet(
            1, title="Отчёт", columns=["day"], rows=[{"day": "1"}], totals={}
        )
    assert service.is_linked(1) is False


@pytest.mark.asyncio
async def test_unreadable_tokens_require_relinking(settings) -> None:
    service = _service(_drive_settings(settings), _google_handler([]))
    other_key = Fernet(generate_encryption_key().encode())
    service.links.upsert(1, "user@example.com", other_key.encrypt(b"{}").decode())

    with pytest.raises(GoogleDriveNotLinkedError, match="заново"):
        await service.create_spreadsheet(
            1, title="Отчёт", columns=["day"], rows=[{"day": "1"}], totals={}
        )


@pytest.mark.asyncio
async def test_refresh_does_not_resurrect_an_unlinked_account(settings) -> None:
    service = _service(_drive_settings(settings), _google_handler([]))
    await _link(service)
    link = service.links.get(1)
    assert link is not None
    payload = service._decrypt(link.encrypted_tokens)
    payload["expires_at"] = (datetime.now(UTC) - timedelta(minutes=5)).isoformat()
    service.links.upsert(1, link.email, service._encrypt(payload))
    # The user taps "unlink" while the report is still being generated.
    service.unlink(1)

    with pytest.raises(GoogleDriveNotLinkedError):
        await service.create_spreadsheet(
            1, title="Отчёт", columns=["day"], rows=[{"day": "1"}], totals={}
        )
    assert service.is_linked(1) is False


@pytest.mark.asyncio
async def test_rejected_sheets_call_names_the_google_reason(settings, caplog) -> None:
    handler = _google_handler(
        [],
        sheets_status=403,
        sheets_error={
            "error": {
                "code": 403,
                "status": "PERMISSION_DENIED",
                "details": [
                    {
                        "@type": "type.googleapis.com/google.rpc.ErrorInfo",
                        "reason": "SERVICE_DISABLED",
                        "metadata": {"consumer": "projects/268835567929"},
                    }
                ],
            }
        },
    )
    service = _service(_drive_settings(settings), handler)
    await _link(service)

    with (
        caplog.at_level(logging.WARNING, logger="app.google_drive.service"),
        pytest.raises(GoogleDriveError) as error,
    ):
        await service.create_spreadsheet(
            1, title="Отчёт", columns=["day"], rows=[{"day": "1"}], totals={}
        )

    technical_message = str(error.value.technical_message)
    assert "status=403" in technical_message
    assert "reason=SERVICE_DISABLED" in technical_message
    assert "consumer=projects/268835567929" in technical_message
    assert "reason=SERVICE_DISABLED" in caplog.text
    assert "access-1" not in caplog.text


@pytest.mark.asyncio
async def test_failed_write_deletes_the_partial_spreadsheet(settings) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        url = str(request.url)
        if url.startswith(TOKEN_ENDPOINT):
            return httpx.Response(
                200,
                json={
                    "access_token": "access-1",
                    "refresh_token": "refresh-1",
                    "expires_in": 3600,
                    "scope": f"openid email {DRIVE_FILE_SCOPE}",
                },
            )
        if url.startswith(USERINFO_ENDPOINT):
            return httpx.Response(200, json={"email": "user@example.com"})
        if request.method == "POST" and url.startswith(SHEETS_ENDPOINT):
            return httpx.Response(
                200, json={"spreadsheetId": "sheet-1", "spreadsheetUrl": SPREADSHEET_URL}
            )
        if request.method == "PUT":
            return httpx.Response(500, json={"error": {"status": "INTERNAL"}})
        return httpx.Response(200, json={})

    service = _service(_drive_settings(settings), handler)
    await _link(service)

    with pytest.raises(GoogleDriveError):
        await service.create_spreadsheet(
            1, title="Отчёт", columns=["day"], rows=[{"day": "1"}], totals={}
        )

    deleted = [
        request
        for request in requests
        if request.method == "DELETE" and str(request.url).startswith(DRIVE_FILES_ENDPOINT)
    ]
    assert len(deleted) == 1
    assert str(deleted[0].url).endswith("/sheet-1")


def test_unlink_removes_the_link(settings) -> None:
    service = _service(_drive_settings(settings), _google_handler([]))
    service.links.upsert(1, "user@example.com", service._encrypt({"refresh_token": "r"}))

    assert service.unlink(1) is True
    assert service.unlink(1) is False
    assert service.is_linked(1) is False


def test_format_keyboard_hides_sheets_until_linked() -> None:
    without = format_keyboard("report")
    with_sheets = format_keyboard("report", include_sheets=True)

    def callbacks(markup) -> list[str]:
        return [button.callback_data for row in markup.inline_keyboard for button in row]

    assert "report:format:sheets" not in callbacks(without)
    assert "report:format:sheets" in callbacks(with_sheets)
    assert allowed_formats() == {"table", "csv", "xlsx"}
    assert allowed_formats(include_sheets=True) == {"table", "csv", "xlsx", "sheets"}


def test_unlinked_user_cannot_select_sheets_format() -> None:
    unlinked = SimpleNamespace(sheets_available=lambda _user: False)
    linked = SimpleNamespace(sheets_available=lambda _user: True)

    with pytest.raises(BotInputError, match="Google Drive не подключён"):
        _parse_format("sheets", _admin(), unlinked)  # type: ignore[arg-type]
    assert _parse_format("sheets", _admin(), linked) == OutputFormat.SHEETS  # type: ignore[arg-type]
    with pytest.raises(BotInputError, match="Неизвестный формат"):
        _parse_format("pdf", _admin(), linked)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_report_is_uploaded_to_google_sheets(settings) -> None:
    drive_settings = _drive_settings(settings)
    requests: list[httpx.Request] = []
    google = _service(drive_settings, _google_handler(requests))
    await _link(google)
    services = build_services(drive_settings, httpx.AsyncClient())
    service = BotReportService(
        settings=drive_settings,
        orchestrator=services["orchestrator"],
        retrieval=services["retrieval"],
        planner=services["planner"],
        validator=services["validator"],
        metrics=services["metrics"],
        resolver=services["resolver"],
        files=services["files"],
        user_access=services["user_access"],
        google_drive=google,
    )

    result = await service.run(
        _admin(),
        f"Покажи выручку за июнь 2026 по юниту {UNIT_ID}",
        OutputFormat.SHEETS,
    )

    assert result.status == "ready"
    assert result.sheet_url == SPREADSHEET_URL
    assert result.text.startswith(f"Google Sheets: {SPREADSHEET_URL}")
    # A Sheets report must not leave a local export file behind.
    assert result.file_path is None
    assert result.report_id is None
    assert service.sheets_available(_admin()) is True


@pytest.mark.asyncio
async def test_sheets_report_is_refused_before_fetching_data(settings) -> None:
    drive_settings = _drive_settings(settings)
    google = _service(drive_settings, _google_handler([]))
    services = build_services(drive_settings, httpx.AsyncClient())
    service = BotReportService(
        settings=drive_settings,
        orchestrator=services["orchestrator"],
        retrieval=services["retrieval"],
        planner=services["planner"],
        validator=services["validator"],
        metrics=services["metrics"],
        resolver=services["resolver"],
        files=services["files"],
        user_access=services["user_access"],
        google_drive=google,
    )

    with pytest.raises(BotInputError, match="Google Drive не подключён"):
        await service.run(_admin(), "Покажи выручку за июнь 2026", OutputFormat.SHEETS)
    assert service.sheets_available(_admin()) is False


@pytest.mark.asyncio
async def test_sheets_report_is_refused_when_integration_is_unconfigured(settings) -> None:
    services = build_services(settings, httpx.AsyncClient())
    service = BotReportService(
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

    with pytest.raises(BotInputError, match="не настроена"):
        await service.run(_admin(), "Покажи выручку за июнь 2026", OutputFormat.SHEETS)


@pytest.mark.asyncio
async def test_drive_status_and_unlink_handlers(settings) -> None:
    service = _service(_drive_settings(settings), _google_handler([]))
    await _link(service)

    status = FakeCallback("drive:show")
    await drive_show_callback(status, FakeState(), _admin(), service)  # type: ignore[arg-type]
    assert "user@example.com" in status.message.answers[-1][0]

    unlink = FakeCallback("drive:unlink")
    await drive_unlink_callback(unlink, _admin(), service)  # type: ignore[arg-type]
    assert "отключён" in unlink.message.answers[-1][0]

    again = FakeCallback("drive:show")
    await drive_show_callback(again, FakeState(), _admin(), service)  # type: ignore[arg-type]
    assert "не подключён" in again.message.answers[-1][0]


@pytest.mark.asyncio
async def test_drive_link_handler_sends_an_authorization_button(settings) -> None:
    service = _service(_drive_settings(settings), _google_handler([]))
    callback = FakeCallback("drive:link")

    await drive_link_callback(callback, _admin(), service)  # type: ignore[arg-type]

    _text, markup = callback.message.answers[-1]
    assert markup is not None
    assert markup.inline_keyboard[0][0].url.startswith("https://accounts.google.com/")


@pytest.mark.asyncio
async def test_drive_handlers_report_missing_configuration(settings) -> None:
    service = _service(settings, _google_handler([]))
    callback = FakeCallback("drive:show")

    await drive_show_callback(callback, FakeState(), _admin(), service)  # type: ignore[arg-type]

    assert "не настроена" in callback.message.answers[-1][0]


def test_compatibility_api_refuses_the_sheets_format(settings) -> None:
    app = create_app(_drive_settings(settings, compatibility_api_key="test-key"))
    with TestClient(app) as client:
        client.headers["x-api-key"] = "test-key"

        response = client.post(
            "/api/reports",
            json={
                "query": f"Покажи выручку за июнь 2026 по юниту {UNIT_ID}",
                "output_format": "sheets",
            },
        )

        assert response.status_code == 422
        assert "sheets" in response.text


@pytest.mark.asyncio
async def test_scheduler_disables_a_sheets_subscription_after_unlinking(settings) -> None:
    database = SQLiteDatabase(settings.sqlite_path)
    database.initialize()
    repository = WeeklyReportRepository(database)
    subscription = repository.create(
        telegram_id=1,
        chat_id=99,
        metric_id="sales",
        unit_id=UNIT_ID,
        output_format="sheets",
        granularity="total",
        weekday=0,
        local_hour=9,
        timezone_name="Europe/Moscow",
        now_utc=datetime(2026, 8, 3, 5, 0, tzinfo=UTC),
    )
    bot = SimpleNamespace(send_message=AsyncMock(), send_document=AsyncMock())
    report_service = SimpleNamespace(
        user_access=SimpleNamespace(authorized=lambda _id: _admin()),
        sheets_available=lambda _user: False,
        run_selected=AsyncMock(),
    )
    scheduler = WeeklyReportScheduler(bot, repository, report_service, poll_seconds=30)  # type: ignore[arg-type]

    await scheduler.run_once(datetime(2026, 8, 10, 6, 0, tzinfo=UTC))

    report_service.run_selected.assert_not_awaited()
    bot.send_message.assert_awaited_once()
    assert "Google Drive" in bot.send_message.await_args.args[1]
    assert repository.list_for_user(1) == []
    assert repository.get(subscription.id).enabled is False


@pytest.mark.asyncio
async def test_scheduler_builds_and_uploads_a_linked_sheets_report(settings) -> None:
    database = SQLiteDatabase(settings.sqlite_path)
    database.initialize()
    repository = WeeklyReportRepository(database)
    subscription = repository.create(
        telegram_id=1,
        chat_id=99,
        metric_id="sales",
        unit_id=UNIT_ID,
        output_format="sheets",
        granularity="total",
        weekday=0,
        local_hour=9,
        timezone_name="Europe/Moscow",
        now_utc=datetime(2026, 8, 3, 5, 0, tzinfo=UTC),
    )
    bot = SimpleNamespace(send_message=AsyncMock(), send_document=AsyncMock())
    google_drive = SimpleNamespace(
        create_spreadsheet=AsyncMock(return_value=(SPREADSHEET_URL, "sheet-1"))
    )
    report_service = SimpleNamespace(
        user_access=SimpleNamespace(authorized=lambda _id: _admin()),
        sheets_available=lambda _user: True,
        google_drive=google_drive,
        run_selected=AsyncMock(
            return_value=SimpleNamespace(
                status="ready",
                text="Готово",
                response={
                    "columns": ["date", "sales"],
                    "rows": [{"date": "2026-08-03", "sales": 10}],
                    "totals": {"sales": 10},
                    "plan": {"metrics": ["sales"]},
                },
                file_path=None,
                report_id=None,
            )
        ),
    )
    scheduler = WeeklyReportScheduler(bot, repository, report_service, poll_seconds=30)  # type: ignore[arg-type]

    await scheduler.run_once(datetime(2026, 8, 10, 6, 0, tzinfo=UTC))

    assert report_service.run_selected.await_args.kwargs["output_format"] == OutputFormat.TABLE
    google_drive.create_spreadsheet.assert_awaited_once()
    bot.send_message.assert_awaited_once_with(99, f"Google Sheets: {SPREADSHEET_URL}\n\nГотово")
    assert repository.get(subscription.id).spreadsheet_id == "sheet-1"


def test_oauth_callback_is_reachable_without_the_api_key(settings) -> None:
    app = create_app(_drive_settings(settings, compatibility_api_key="test-key"))
    with TestClient(app) as client:
        assert client.get("/api/metrics").status_code == 401

        missing_code = client.get("/google/oauth/callback")
        assert missing_code.status_code == 400
        assert "Подключение не завершено" in missing_code.text

        forged = client.get("/google/oauth/callback", params={"code": "c", "state": "forged"})
        assert forged.status_code == 400
        assert "недействительна" in forged.text


def test_oauth_callback_completes_the_link(settings) -> None:
    drive_settings = _drive_settings(settings, compatibility_api_key="test-key")
    app = create_app(drive_settings)
    with TestClient(app) as client:
        service: GoogleDriveService = app.state.services["google_drive"]
        service.http_client = httpx.AsyncClient(transport=httpx.MockTransport(_google_handler([])))
        url = service.create_auth_url(42)
        state = parse_qs(urlparse(url).query)["state"][0]

        response = client.get("/google/oauth/callback", params={"code": "auth", "state": state})

        assert response.status_code == 200
        assert "user@example.com" in response.text
        assert service.is_linked(42) is True
