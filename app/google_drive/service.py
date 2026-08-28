from __future__ import annotations

import json
import logging
import math
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import quote, urlencode

import httpx
from cryptography.fernet import Fernet, InvalidToken

from app.config import Settings
from app.errors import ConfigurationError, GoogleDriveError, GoogleDriveNotLinkedError
from app.storage.google_drive import GoogleDriveLinkRepository, OAuthStateRepository

AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
USERINFO_ENDPOINT = "https://openidconnect.googleapis.com/v1/userinfo"
SHEETS_ENDPOINT = "https://sheets.googleapis.com/v4/spreadsheets"
DRIVE_FILES_ENDPOINT = "https://www.googleapis.com/drive/v3/files"
# drive.file limits the grant to files this application creates itself.
DRIVE_FILE_SCOPE = "https://www.googleapis.com/auth/drive.file"
SCOPES = ("openid", "email", DRIVE_FILE_SCOPE)
SHEET_TITLE = "Отчёт"
VALUES_CHUNK_ROWS = 2000
ACCESS_TOKEN_LEEWAY_SECONDS = 60
REQUEST_TIMEOUT_SECONDS = 30.0
TOTAL_LABEL = "Итого"
TIME_FIELDS = ("hour", "day", "week", "month")
ENTITY_FIELDS = ("unitName", "unit_name", "unitId", "unit_id")
EXTRA_FIELDS = ("salesChannel", "ingredient", "ingredientCategory", "stopReason")
DIMENSION_FIELDS = frozenset((*TIME_FIELDS, *ENTITY_FIELDS, *EXTRA_FIELDS))

logger = logging.getLogger(__name__)


def generate_encryption_key() -> str:
    return Fernet.generate_key().decode()


class GoogleDriveService:
    def __init__(
        self,
        settings: Settings,
        links: GoogleDriveLinkRepository,
        states: OAuthStateRepository,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self.settings = settings
        self.links = links
        self.states = states
        # Google must not share the Dodo client: in mock mode that client answers every
        # request from a local mock transport.
        self.http_client = http_client
        self._owned_client: httpx.AsyncClient | None = None
        self._cipher: Fernet | None = None

    def _client(self) -> httpx.AsyncClient:
        if self.http_client is not None:
            return self.http_client
        if self._owned_client is None:
            self._owned_client = httpx.AsyncClient(timeout=httpx.Timeout(REQUEST_TIMEOUT_SECONDS))
        return self._owned_client

    async def aclose(self) -> None:
        if self._owned_client is not None:
            await self._owned_client.aclose()
            self._owned_client = None

    @property
    def enabled(self) -> bool:
        return self.settings.google_drive_enabled

    def is_linked(self, telegram_id: int) -> bool:
        return self.enabled and self.links.get(telegram_id) is not None

    def linked_email(self, telegram_id: int) -> str | None:
        if not self.enabled:
            return None
        link = self.links.get(telegram_id)
        return link.email if link is not None else None

    def unlink(self, telegram_id: int) -> bool:
        return self.links.delete(telegram_id)

    def create_auth_url(self, telegram_id: int) -> str:
        self._ensure_enabled()
        # Fail before redirecting the user when the encryption key cannot be used.
        self._fernet()
        state = secrets.token_urlsafe(32)
        self.states.create(state, telegram_id)
        parameters = {
            "client_id": self.settings.google_client_id,
            "redirect_uri": self.settings.google_redirect_uri,
            "response_type": "code",
            "scope": " ".join(SCOPES),
            "access_type": "offline",
            "prompt": "consent",
            "include_granted_scopes": "true",
            "state": state,
        }
        return f"{AUTH_ENDPOINT}?{urlencode(parameters)}"

    async def complete_link(self, code: str, state: str) -> tuple[int, str]:
        self._ensure_enabled()
        telegram_id = self.states.consume(state)
        if telegram_id is None:
            raise GoogleDriveError("Ссылка подключения недействительна или устарела.")
        tokens = await self._request_tokens(
            {
                "code": code,
                "client_id": self.settings.google_client_id,
                "client_secret": self.settings.google_client_secret,
                "redirect_uri": self.settings.google_redirect_uri,
                "grant_type": "authorization_code",
            }
        )
        refresh_token = str(tokens.get("refresh_token") or "")
        if not refresh_token:
            raise GoogleDriveError(
                "Google не вернул токен обновления. Отключите приложение в настройках "
                "Google-аккаунта и повторите подключение."
            )
        # Granular permissions let the user clear the Drive checkbox and still return a code.
        if DRIVE_FILE_SCOPE not in str(tokens.get("scope") or "").split():
            raise GoogleDriveError(
                "Не выдан доступ к созданию файлов на Google Диске. Повторите подключение "
                "и оставьте отмеченным разрешение для Google Диска."
            )
        access_token = str(tokens.get("access_token") or "")
        email = await self._fetch_email(access_token)
        self.links.upsert(
            telegram_id,
            email,
            self._encrypt(
                {
                    "refresh_token": refresh_token,
                    "access_token": access_token,
                    "expires_at": self._expires_at(tokens).isoformat(),
                }
            ),
        )
        logger.info("google_drive_linked telegram_id=%s", telegram_id)
        return telegram_id, email

    async def append_to_spreadsheet(
        self,
        telegram_id: int,
        spreadsheet_id: str,
        *,
        period_label: str,
        columns: list[str],
        rows: list[dict[str, Any]],
        totals: dict[str, Any],
    ) -> str:
        """Add a new period as date columns to the right of the existing matrix.

        Returns the spreadsheet URL. Raises GoogleDriveError if the spreadsheet
        no longer exists, cannot be accessed, or is not a matrix sheet.
        """
        self._ensure_enabled()
        incoming = self._build_values(columns, rows, totals, period_label=period_label)
        if not _is_matrix(incoming):
            raise GoogleDriveError(
                "Новые данные нельзя добавить колонкой. Создаётся новая таблица."
            )
        access_token = await self._access_token(telegram_id)
        headers = {"Authorization": f"Bearer {access_token}"}
        range_path = _values_range("A1:ZZ")

        try:
            payload = await self._json_request(
                "GET",
                f"{SHEETS_ENDPOINT}/{spreadsheet_id}/values/{range_path}",
                headers=headers,
            )
        except GoogleDriveError as exc:
            if "404" in str(exc.technical_message or ""):
                raise GoogleDriveError(
                    "Таблица Google Sheets была удалена. Создаётся новая таблица."
                ) from exc
            raise

        existing = payload.get("values") if isinstance(payload, dict) else None
        if not existing:
            merged = incoming
        elif not _is_matrix(existing):
            raise GoogleDriveError("Таблица имеет другой формат. Создаётся новая таблица.")
        else:
            merged = _merge_matrices(existing, incoming)

        merged = _overwrite_bounds(merged, existing or [])
        await self._write_values(spreadsheet_id, headers, merged)
        logger.info(
            "google_sheet_appended telegram_id=%s spreadsheet_id=%s rows=%s columns=%s",
            telegram_id,
            spreadsheet_id,
            max(len(merged) - 1, 0),
            max(len(merged[0]) - 1, 0) if merged else 0,
        )
        return f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}"

    async def create_spreadsheet(
        self,
        telegram_id: int,
        *,
        title: str,
        columns: list[str],
        rows: list[dict[str, Any]],
        totals: dict[str, Any],
        period_label: str | None = None,
    ) -> tuple[str, str]:
        """Create a new Google Sheets spreadsheet.

        Returns a tuple of (spreadsheet_url, spreadsheet_id).
        """
        self._ensure_enabled()
        values = self._build_values(columns, rows, totals, period_label=period_label)
        access_token = await self._access_token(telegram_id)
        headers = {"Authorization": f"Bearer {access_token}"}
        column_count = max(len(values[0]), 1) if values else 1
        created = await self._json_request(
            "POST",
            SHEETS_ENDPOINT,
            headers=headers,
            payload={
                "properties": {"title": title},
                "sheets": [
                    {
                        "properties": {
                            "title": SHEET_TITLE,
                            "gridProperties": {
                                "rowCount": max(len(values), 1),
                                "columnCount": column_count,
                            },
                        }
                    }
                ],
            },
        )
        spreadsheet_id = str(created.get("spreadsheetId") or "")
        if not spreadsheet_id:
            raise GoogleDriveError("Google не вернул идентификатор таблицы.")
        try:
            await self._write_values(spreadsheet_id, headers, values)
        except Exception:
            await self._discard_spreadsheet(spreadsheet_id, headers)
            raise
        logger.info(
            "google_sheet_created telegram_id=%s spreadsheet_id=%s rows=%s",
            telegram_id,
            spreadsheet_id,
            max(len(values) - 1, 0),
        )
        url = (
            str(created.get("spreadsheetUrl") or "")
            or f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}"
        )
        return url, spreadsheet_id

    async def _write_values(
        self, spreadsheet_id: str, headers: dict[str, str], values: list[list[Any]]
    ) -> None:
        for offset in range(0, len(values), VALUES_CHUNK_ROWS):
            chunk = values[offset : offset + VALUES_CHUNK_ROWS]
            cell = f"A{offset + 1}"
            await self._json_request(
                "PUT",
                f"{SHEETS_ENDPOINT}/{spreadsheet_id}/values/{_values_range(cell)}",
                headers=headers,
                payload={"values": chunk},
                params={"valueInputOption": "RAW"},
            )

    async def _discard_spreadsheet(self, spreadsheet_id: str, headers: dict[str, str]) -> None:
        try:
            await self._client().delete(f"{DRIVE_FILES_ENDPOINT}/{spreadsheet_id}", headers=headers)
        except httpx.HTTPError:
            logger.warning("google_sheet_cleanup_failed spreadsheet_id=%s", spreadsheet_id)

    def _build_values(
        self,
        columns: list[str],
        rows: list[dict[str, Any]],
        totals: dict[str, Any],
        *,
        period_label: str | None = None,
    ) -> list[list[Any]]:
        if not columns:
            raise GoogleDriveError("В отчёте нет колонок для выгрузки.")
        if len(rows) > self.settings.google_sheets_max_rows:
            raise GoogleDriveError(
                f"В отчёте {len(rows)} строк, а в Google Sheets выгружается не более "
                f"{self.settings.google_sheets_max_rows}. Выберите формат CSV или XLSX."
            )
        matrix = _build_matrix(columns, rows, totals, period_label=period_label)
        if matrix is not None:
            return matrix
        values: list[list[Any]] = [[str(column) for column in columns]]
        values.extend([_cell(row.get(column)) for column in columns] for row in rows)
        if totals:
            total_row = [_cell(totals.get(column)) for column in columns]
            total_row[0] = TOTAL_LABEL
            values.append(total_row)
        return values

    async def _access_token(self, telegram_id: int) -> str:
        link = self.links.get(telegram_id)
        if link is None:
            raise GoogleDriveNotLinkedError(
                "Google Drive не подключён. Откройте /drive, чтобы подключить аккаунт."
            )
        payload = self._decrypt(link.encrypted_tokens)
        access_token = str(payload.get("access_token") or "")
        if access_token and self._token_is_fresh(payload.get("expires_at")):
            return access_token
        refresh_token = str(payload.get("refresh_token") or "")
        if not refresh_token:
            self.links.delete(telegram_id)
            raise GoogleDriveNotLinkedError(
                "Сохранённый доступ к Google Drive повреждён. Подключите аккаунт заново."
            )
        tokens = await self._request_tokens(
            {
                "refresh_token": refresh_token,
                "client_id": self.settings.google_client_id,
                "client_secret": self.settings.google_client_secret,
                "grant_type": "refresh_token",
            },
            revoke_for=telegram_id,
        )
        access_token = str(tokens.get("access_token") or "")
        if not access_token:
            raise GoogleDriveError("Google не вернул токен доступа.")
        payload["access_token"] = access_token
        payload["expires_at"] = self._expires_at(tokens).isoformat()
        if tokens.get("refresh_token"):
            payload["refresh_token"] = str(tokens["refresh_token"])
        if not self.links.update_tokens(telegram_id, self._encrypt(payload)):
            raise GoogleDriveNotLinkedError(
                "Google Drive был отключён во время формирования отчёта."
            )
        return access_token

    async def _request_tokens(
        self, data: dict[str, str], *, revoke_for: int | None = None
    ) -> dict[str, Any]:
        try:
            response = await self._client().post(TOKEN_ENDPOINT, data=data)
        except httpx.HTTPError as exc:
            raise GoogleDriveError(
                "Google не отвечает. Повторите попытку позже.", technical_message=str(exc)
            ) from exc
        if response.status_code >= 400:
            reason = self._error_reason(response)
            if reason == "invalid_grant" and revoke_for is not None:
                self.links.delete(revoke_for)
                raise GoogleDriveNotLinkedError(
                    "Доступ к Google Drive отозван. Подключите аккаунт заново через /drive."
                )
            raise GoogleDriveError(
                "Google отклонил авторизацию.",
                technical_message=f"status={response.status_code} error={reason}",
            )
        return self._parse_json(response)

    async def _fetch_email(self, access_token: str) -> str:
        payload = await self._json_request(
            "GET",
            USERINFO_ENDPOINT,
            headers={"Authorization": f"Bearer {access_token}"},
        )
        email = str(payload.get("email") or "").strip()
        if not email:
            raise GoogleDriveError("Google не вернул адрес электронной почты.")
        return email

    async def _json_request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str],
        payload: dict[str, Any] | None = None,
        params: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        try:
            response = await self._client().request(
                method, url, headers=headers, json=payload, params=params
            )
        except httpx.HTTPError as exc:
            raise GoogleDriveError(
                "Google не отвечает. Повторите попытку позже.", technical_message=str(exc)
            ) from exc
        if response.status_code >= 400:
            reason = self._error_reason(response)
            technical_message = f"status={response.status_code} error={reason}"
            details = self._error_details(response)
            if details:
                technical_message = f"{technical_message} {details}"
            logger.warning(
                "google_api_rejected method=%s host=%s %s",
                method,
                httpx.URL(url).host,
                technical_message,
            )
            raise GoogleDriveError(
                "Google отклонил запрос к таблицам.",
                technical_message=technical_message,
            )
        return self._parse_json(response)

    @staticmethod
    def _parse_json(response: httpx.Response) -> dict[str, Any]:
        try:
            payload = response.json()
        except ValueError as exc:
            raise GoogleDriveError("Google вернул некорректный ответ.") from exc
        if not isinstance(payload, dict):
            raise GoogleDriveError("Google вернул некорректный ответ.")
        return payload

    @staticmethod
    def _error_reason(response: httpx.Response) -> str:
        """Extract only the machine-readable reason so tokens never reach the logs."""
        try:
            payload = response.json()
        except ValueError:
            return "unknown"
        if not isinstance(payload, dict):
            return "unknown"
        error = payload.get("error")
        if isinstance(error, dict):
            return str(error.get("status") or error.get("code") or "unknown")
        return str(error or "unknown")

    @staticmethod
    def _error_details(response: httpx.Response) -> str:
        """Read ErrorInfo, where Google separates causes that share one status.

        PERMISSION_DENIED covers both a disabled API and an insufficient grant; only
        `reason` tells them apart, and `consumer` names the project that was billed.
        """
        try:
            payload = response.json()
        except ValueError:
            return ""
        error = payload.get("error") if isinstance(payload, dict) else None
        if not isinstance(error, dict):
            return ""
        for detail in error.get("details") or []:
            if not isinstance(detail, dict):
                continue
            parts = []
            reason = str(detail.get("reason") or "").strip()
            if reason:
                parts.append(f"reason={reason}")
            metadata = detail.get("metadata")
            if isinstance(metadata, dict):
                consumer = str(metadata.get("consumer") or "").strip()
                if consumer:
                    parts.append(f"consumer={consumer}")
            if parts:
                return " ".join(parts)
        return ""

    @staticmethod
    def _expires_at(tokens: dict[str, Any]) -> datetime:
        try:
            seconds = int(tokens.get("expires_in") or 0)
        except (TypeError, ValueError):
            seconds = 0
        return datetime.now(UTC) + timedelta(seconds=max(seconds, 0))

    @staticmethod
    def _token_is_fresh(expires_at: Any) -> bool:
        if not expires_at:
            return False
        try:
            deadline = datetime.fromisoformat(str(expires_at))
        except ValueError:
            return False
        if deadline.tzinfo is None:
            deadline = deadline.replace(tzinfo=UTC)
        return deadline - timedelta(seconds=ACCESS_TOKEN_LEEWAY_SECONDS) > datetime.now(UTC)

    def _encrypt(self, payload: dict[str, Any]) -> str:
        raw = json.dumps(payload, ensure_ascii=False).encode()
        return self._fernet().encrypt(raw).decode()

    def _decrypt(self, value: str) -> dict[str, Any]:
        try:
            raw = self._fernet().decrypt(value.encode())
        except InvalidToken as exc:
            raise GoogleDriveNotLinkedError(
                "Сохранённый доступ к Google Drive не читается. Подключите аккаунт заново."
            ) from exc
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise GoogleDriveNotLinkedError("Сохранённый доступ к Google Drive повреждён.")
        return payload

    def _fernet(self) -> Fernet:
        if self._cipher is None:
            try:
                self._cipher = Fernet(self.settings.google_token_encryption_key.encode())
            except (TypeError, ValueError) as exc:
                raise ConfigurationError(
                    "Некорректный GOOGLE_TOKEN_ENCRYPTION_KEY: ожидается ключ Fernet"
                ) from exc
        return self._cipher

    def _ensure_enabled(self) -> None:
        if not self.enabled:
            raise GoogleDriveError("Интеграция с Google Drive не настроена.")


def _values_range(cell: str) -> str:
    return quote(f"'{SHEET_TITLE}'!{cell}", safe="")


def _cell(value: Any) -> Any:
    if value is None:
        return ""
    if isinstance(value, (bool, str)):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    return str(value)


def _is_matrix(values: list[list[Any]]) -> bool:
    return bool(values) and (not values[0] or values[0][0] in {"", None})


def _build_matrix(
    columns: list[str],
    rows: list[dict[str, Any]],
    totals: dict[str, Any] | None = None,
    *,
    period_label: str | None = None,
) -> list[list[Any]] | None:
    value_fields = [column for column in columns if column not in DIMENSION_FIELDS]
    has_entity = any(field in columns for field in ENTITY_FIELDS)
    if not value_fields or not has_entity:
        return None

    metric = value_fields[0]
    default_header = _period_header(period_label) or str(metric)
    date_order: list[str] = []
    date_seen: set[str] = set()
    unit_order: list[str] = []
    unit_seen: set[str] = set()
    cells: dict[tuple[str, str], Any] = {}

    for row in rows:
        unit = _row_label(row)
        date_header = _row_date_header(row) or default_header
        if unit not in unit_seen:
            unit_seen.add(unit)
            unit_order.append(unit)
        if date_header not in date_seen:
            date_seen.add(date_header)
            date_order.append(date_header)
        cells[(unit, date_header)] = _cell(row.get(metric))

    if not date_order:
        date_order = [default_header]
    if not unit_order:
        unit_order = ["Все заведения"]

    header: list[Any] = ["", *date_order]
    values: list[list[Any]] = [header]
    column_totals = [0.0] * len(date_order)
    has_numeric = [False] * len(date_order)
    for unit in unit_order:
        line: list[Any] = [unit]
        for index, date_header in enumerate(date_order):
            value = cells.get((unit, date_header), "")
            line.append(value)
            number = _numeric(value)
            if number is not None:
                column_totals[index] += number
                has_numeric[index] = True
        values.append(line)

    total_row: list[Any] = [TOTAL_LABEL]
    report_totals = totals or {}
    if len(date_order) == 1 and metric in report_totals:
        total_row.append(_cell(report_totals.get(metric)))
    else:
        for index, total in enumerate(column_totals):
            total_row.append(round(total, 2) if has_numeric[index] else "")
    values.append(total_row)
    return values


def _merge_matrices(existing: list[list[Any]], incoming: list[list[Any]]) -> list[list[Any]]:
    existing_dates = [str(value) for value in existing[0][1:]]
    incoming_dates = [str(value) for value in incoming[0][1:]]
    dates = list(existing_dates)
    seen = set(existing_dates)
    for date_header in incoming_dates:
        if date_header not in seen:
            seen.add(date_header)
            dates.append(date_header)

    cells: dict[tuple[str, str], Any] = {}
    units: list[str] = []
    unit_seen: set[str] = set()
    for source in (existing, incoming):
        source_dates = [str(value) for value in source[0][1:]]
        for row in source[1:]:
            if not row:
                continue
            unit = str(row[0] or "").strip()
            if not unit or unit == TOTAL_LABEL:
                continue
            if unit not in unit_seen:
                unit_seen.add(unit)
                units.append(unit)
            for index, date_header in enumerate(source_dates):
                if index + 1 >= len(row):
                    break
                value = row[index + 1]
                if value not in {"", None}:
                    cells[(unit, date_header)] = value

    header: list[Any] = ["", *dates]
    values: list[list[Any]] = [header]
    for unit in units:
        line: list[Any] = [unit]
        for date_header in dates:
            line.append(cells.get((unit, date_header), ""))
        values.append(line)

    preserved = {**_totals_by_date(existing), **_totals_by_date(incoming)}
    total_row: list[Any] = [TOTAL_LABEL]
    for date_header in dates:
        if date_header in preserved:
            total_row.append(preserved[date_header])
        else:
            total_row.append("")
    values.append(total_row)
    return values


def _totals_by_date(matrix: list[list[Any]]) -> dict[str, Any]:
    if not matrix:
        return {}
    dates = [str(value) for value in matrix[0][1:]]
    for row in reversed(matrix[1:]):
        if row and str(row[0] or "").strip() == TOTAL_LABEL:
            result: dict[str, Any] = {}
            for index, date_header in enumerate(dates):
                if index + 1 < len(row):
                    result[date_header] = row[index + 1]
            return result
    return {}


def _overwrite_bounds(merged: list[list[Any]], existing: list[list[Any]]) -> list[list[Any]]:
    """Pad with empty cells so a smaller rewrite clears leftover values."""
    existing_rows = len(existing)
    existing_cols = max((len(row) for row in existing), default=0)
    merged_cols = max((len(row) for row in merged), default=0)
    columns = max(merged_cols, existing_cols)
    padded: list[list[Any]] = []
    for row in merged:
        padded.append(list(row) + [""] * (columns - len(row)))
    while len(padded) < existing_rows:
        padded.append([""] * columns)
    return padded


def _row_label(row: dict[str, Any]) -> str:
    name = ""
    for field in ENTITY_FIELDS:
        name = str(row.get(field) or "").strip()
        if name:
            break
    extras = [
        str(row.get(field) or "").strip()
        for field in EXTRA_FIELDS
        if str(row.get(field) or "").strip()
    ]
    parts = [part for part in [name, *extras] if part]
    return " / ".join(parts) or "Все заведения"


def _row_date_header(row: dict[str, Any]) -> str:
    for field in TIME_FIELDS:
        raw = row.get(field)
        if raw not in {None, ""}:
            return _format_date_header(raw)
    return ""


def _period_header(period_label: str | None) -> str:
    if not period_label:
        return ""
    text = period_label.strip()
    for separator in (" — ", " – ", " - "):
        if separator in text:
            text = text.rsplit(separator, 1)[-1].strip()
            break
    return _format_date_header(text) or text


def _format_date_header(value: Any) -> str:
    text = str(value).strip()
    if not text:
        return ""
    if "T" in text:
        date_part, time_part = text.split("T", 1)
        formatted = _iso_date_to_display(date_part)
        hour = time_part[:5] if len(time_part) >= 5 else ""
        return f"{formatted} {hour}".strip() if formatted else text
    formatted = _iso_date_to_display(text[:10])
    return formatted or text


def _iso_date_to_display(value: str) -> str:
    try:
        return datetime.strptime(value, "%Y-%m-%d").strftime("%d.%m.%Y")
    except ValueError:
        return ""


def _numeric(value: Any) -> float | None:
    if isinstance(value, bool) or value in {"", None}:
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        return number if math.isfinite(number) else None
    return None
