from __future__ import annotations

import asyncio
import logging
import random
import re
from collections.abc import Awaitable, Callable
from email.utils import parsedate_to_datetime
from urllib.parse import quote, urlparse

import httpx

from app.documentation.models import EndpointDocument
from app.dodo.profile import ROOT_COLLECTION
from app.dodo.request_counter import RequestCounter
from app.errors import (
    ConfigurationError,
    DodoApiError,
    DodoForbiddenError,
    DodoRateLimitError,
    DodoUnauthorizedError,
    DodoValidationError,
)

logger = logging.getLogger(__name__)
ALLOWED_HOSTS = {"api.dodois.io", "api.dodois.com"}
PLACEHOLDER_RE = re.compile(r"\{([^{}]+)\}")

TokenProvider = Callable[[], Awaitable[str]]


class DodoApiClient:
    def __init__(
        self,
        http_client: httpx.AsyncClient,
        *,
        access_token: str = "",
        country_id: str,
        allowed_operations: set[str],
        max_retries: int = 4,
        allow_all_get: bool = False,
        token_provider: TokenProvider | None = None,
    ) -> None:
        self.http_client = http_client
        self._static_access_token = access_token
        self.country_id = country_id.casefold()
        self.allowed_operations = allowed_operations
        self.max_retries = max_retries
        self.allow_all_get = allow_all_get
        self._token_provider = token_provider
        self._requests = RequestCounter()

    @property
    def access_token(self) -> str:
        """Backward-compatible property for the static access token."""
        return self._static_access_token

    async def _get_access_token(self) -> str:
        """Get the access token, either from provider or static value."""
        if self._token_provider is not None:
            return await self._token_provider()
        if not self._static_access_token:
            raise ConfigurationError("Не задан DODO_ACCESS_TOKEN")
        return self._static_access_token

    @property
    def request_count(self) -> int:
        return self._requests.request_count

    def request_scope(self):
        return self._requests.request_scope()

    def _is_permitted(self, operation: EndpointDocument) -> bool:
        if operation.method != "GET" or operation.deprecated:
            return False
        return self.allow_all_get or operation.operation_id in self.allowed_operations

    def _url(self, operation: EndpointDocument, path_values: dict[str, str] | None = None) -> str:
        if not self._is_permitted(operation):
            raise DodoValidationError("Операция не разрешена для выполнения")
        links = operation.links
        preferred = [
            link
            for link in links
            if f"/{self.country_id}/" in link or f"/{self.country_id}" == urlparse(link).path[-3:]
        ]
        url = (preferred or links or [""])[0]
        url = _substitute_path(url, path_values or {})
        parsed = urlparse(url)
        if parsed.scheme != "https" or parsed.hostname not in ALLOWED_HOSTS:
            raise DodoValidationError("Документация содержит недоверенный URL")
        return url

    async def request_operation(
        self,
        operation: EndpointDocument,
        query_params: dict[str, str | int | bool | list[str]],
        path_values: dict[str, str] | None = None,
    ) -> dict:
        access_token = await self._get_access_token()
        url = self._url(operation, path_values)
        params = {
            key: ",".join(value) if isinstance(value, list) else value
            for key, value in query_params.items()
        }
        headers = {"Authorization": f"Bearer {access_token}"}
        last_error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                self._requests.count_request()
                response = await self.http_client.get(url, params=params, headers=headers)
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                last_error = exc
                if attempt == self.max_retries:
                    break
                await asyncio.sleep(min(2**attempt + random.random(), 10))
                continue
            request_id = response.headers.get("x-request-id") or response.headers.get("request-id")
            details: dict[str, object] = {"operation_id": operation.operation_id}
            if request_id:
                details["dodo_request_id"] = request_id
            if response.status_code == 401:
                raise DodoUnauthorizedError(details=details)
            if response.status_code == 403:
                # Restricted endpoints are an expected outcome once any GET may be
                # called, so name the scope the token is missing.
                raise DodoForbiddenError(
                    details={**details, "required_scopes": operation.scopes},
                )
            if response.status_code in {400, 404}:
                raise DodoValidationError("Dodo IS отклонил параметры запроса", details=details)
            if response.status_code == 429 or response.status_code >= 500:
                if attempt < self.max_retries:
                    delay = _retry_delay(response, attempt)
                    logger.warning(
                        "Dodo retry operation=%s status=%s attempt=%s",
                        operation.operation_id,
                        response.status_code,
                        attempt + 1,
                    )
                    await asyncio.sleep(delay)
                    continue
                if response.status_code == 429:
                    raise DodoRateLimitError(details=details)
                raise DodoApiError("Dodo IS временно недоступен", details=details)
            if not response.is_success:
                raise DodoApiError(f"Dodo IS вернул HTTP {response.status_code}", details=details)
            try:
                data = response.json()
            except ValueError as exc:
                raise DodoApiError("Dodo IS вернул некорректный JSON", details=details) from exc
            if isinstance(data, list):
                # A few catalogues answer with a bare JSON array; wrap it so the
                # rest of the pipeline always works with a mapping.
                return {ROOT_COLLECTION: data}
            if not isinstance(data, dict):
                raise DodoApiError("Dodo IS вернул неожиданный формат", details=details)
            return data
        raise DodoApiError(
            "Не удалось связаться с Dodo IS",
            technical_message=str(last_error),
            details={"operation_id": operation.operation_id},
        )


def _substitute_path(url: str, values: dict[str, str]) -> str:
    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in values:
            raise DodoValidationError(f"Не задан параметр пути «{name}»")
        return quote(str(values[name]), safe="")

    return PLACEHOLDER_RE.sub(replace, url)


def _retry_delay(response: httpx.Response, attempt: int) -> float:
    value = response.headers.get("Retry-After")
    if value:
        try:
            return min(float(value), 30)
        except ValueError:
            try:
                return max(
                    0.0,
                    min(
                        (
                            parsedate_to_datetime(value)
                            - parsedate_to_datetime(response.headers.get("Date", value))
                        ).total_seconds(),
                        30,
                    ),
                )
            except (TypeError, ValueError):
                pass
    return min(2**attempt + random.random(), 10)
