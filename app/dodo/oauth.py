from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from app.config import Settings
from app.errors import ConfigurationError, DodoTokenRefreshError
from app.storage.dodo_tokens import DodoTokenRepository

TOKEN_ENDPOINT = "https://auth.dodois.io/connect/token"
ACCESS_TOKEN_LEEWAY_SECONDS = 60

logger = logging.getLogger(__name__)


class DodoTokenRefresher:
    def __init__(
        self,
        settings: Settings,
        repository: DodoTokenRepository,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self.settings = settings
        self.repository = repository
        self._http_client = http_client

    def _client(self) -> httpx.AsyncClient:
        if self._http_client is not None:
            return self._http_client
        return httpx.AsyncClient(timeout=30.0)

    async def bootstrap(self) -> str:
        """Ensure a valid access token is available, seeding from settings if needed.

        On first run (storage empty), uses `dodo_oauth_refresh_token` from settings
        as the initial seed and performs an immediate refresh. On subsequent runs,
        uses the stored refresh token.

        Returns the current valid access_token.
        """
        self._ensure_enabled()
        stored = self.repository.get()
        if stored is not None and stored.access_token and self._token_is_fresh(stored.expires_at):
            logger.info("dodo_oauth_bootstrap_using_stored_token")
            return stored.access_token

        if stored is not None and stored.refresh_token:
            refresh_token = stored.refresh_token
            logger.info("dodo_oauth_bootstrap_refreshing_stored_token")
        else:
            refresh_token = self.settings.dodo_oauth_refresh_token
            if not refresh_token:
                raise ConfigurationError(
                    "Не задан DODO_OAUTH_REFRESH_TOKEN для начальной инициализации"
                )
            logger.info("dodo_oauth_bootstrap_using_seed_token")

        return await self._do_refresh(refresh_token)

    async def refresh(self) -> str:
        """Refresh the access token using the stored refresh token.

        Returns the new access_token.
        """
        self._ensure_enabled()
        stored = self.repository.get()
        if stored is None or not stored.refresh_token:
            seed = self.settings.dodo_oauth_refresh_token
            if not seed:
                raise ConfigurationError(
                    "Нет сохранённого refresh_token и не задан DODO_OAUTH_REFRESH_TOKEN"
                )
            return await self._do_refresh(seed)
        return await self._do_refresh(stored.refresh_token)

    async def get_access_token(self) -> str:
        """Get a valid access token, refreshing on demand if expired.

        This is the main method called by the token provider for DodoApiClient.
        """
        self._ensure_enabled()
        stored = self.repository.get()
        if stored is not None and stored.access_token and self._token_is_fresh(stored.expires_at):
            return stored.access_token
        return await self.refresh()

    async def _do_refresh(self, refresh_token: str) -> str:
        """Execute the token refresh against Dodo's OAuth endpoint."""
        tokens = await self._request_tokens(refresh_token)
        access_token = str(tokens.get("access_token") or "")
        if not access_token:
            raise DodoTokenRefreshError("Dodo IS не вернул токен доступа")

        new_refresh_token = str(tokens.get("refresh_token") or "") or refresh_token
        expires_at = self._expires_at(tokens)
        now = datetime.now(UTC)

        self.repository.upsert(access_token, new_refresh_token, expires_at, now)
        logger.info(
            "dodo_oauth_token_refreshed expires_at=%s rotated=%s",
            expires_at.isoformat(),
            new_refresh_token != refresh_token,
        )
        return access_token

    async def _request_tokens(self, refresh_token: str) -> dict[str, Any]:
        """POST to the Dodo token endpoint with grant_type=refresh_token."""
        data = {
            "grant_type": "refresh_token",
            "client_id": self.settings.dodo_oauth_client_id,
            "client_secret": self.settings.dodo_oauth_client_secret,
            "refresh_token": refresh_token,
        }
        try:
            response = await self._client().post(TOKEN_ENDPOINT, data=data)
        except httpx.HTTPError as exc:
            raise DodoTokenRefreshError(
                "Dodo IS не отвечает. Повторите попытку позже.", technical_message=str(exc)
            ) from exc

        if response.status_code >= 400:
            reason = self._error_reason(response)
            logger.error(
                "dodo_oauth_refresh_failed status=%s reason=%s",
                response.status_code,
                reason,
            )
            raise DodoTokenRefreshError(
                "Dodo IS отклонил запрос на обновление токена",
                technical_message=f"status={response.status_code} error={reason}",
            )
        return self._parse_json(response)

    def _token_is_fresh(self, expires_at: datetime | None) -> bool:
        if expires_at is None:
            return False
        return datetime.now(UTC) + timedelta(seconds=ACCESS_TOKEN_LEEWAY_SECONDS) < expires_at

    @staticmethod
    def _expires_at(tokens: dict[str, Any]) -> datetime:
        try:
            seconds = int(tokens.get("expires_in") or 0)
        except (TypeError, ValueError):
            seconds = 0
        if seconds <= 0:
            seconds = 3600
        return datetime.now(UTC) + timedelta(seconds=seconds)

    @staticmethod
    def _parse_json(response: httpx.Response) -> dict[str, Any]:
        try:
            payload = response.json()
        except ValueError as exc:
            raise DodoTokenRefreshError("Dodo IS вернул некорректный ответ") from exc
        if not isinstance(payload, dict):
            raise DodoTokenRefreshError("Dodo IS вернул некорректный ответ")
        return payload

    @staticmethod
    def _error_reason(response: httpx.Response) -> str:
        try:
            payload = response.json()
        except ValueError:
            return "unknown"
        if not isinstance(payload, dict):
            return "unknown"
        return str(payload.get("error") or "unknown")

    def _ensure_enabled(self) -> None:
        if not self.settings.dodo_oauth_enabled:
            raise ConfigurationError("Dodo OAuth не настроен")
