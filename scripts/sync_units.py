from __future__ import annotations

import asyncio
import json

import httpx

from app.config import get_settings
from app.documentation import DocumentationLoader, DocumentationRepository
from app.dodo.client import DodoApiClient
from app.dodo.paginator import paginate
from app.errors import ConfigurationError
from app.storage.sqlite import SQLiteDatabase


async def run() -> None:
    settings = get_settings()
    if not settings.dodo_access_token or not settings.dodo_business_id:
        raise ConfigurationError(
            "Для синхронизации нужны DODO_ACCESS_TOKEN, DODO_COUNTRY_ID и DODO_BUSINESS_ID"
        )
    repository = DocumentationRepository(
        SQLiteDatabase(settings.sqlite_path),
        DocumentationLoader(settings.documentation_zip_path),
        allowed_operations=settings.allowed_operations,
        index_all_get=settings.index_all_get_operations,
    )
    repository.ensure_index()
    operation = repository.get("get-all-units")
    assert operation is not None
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(settings.dodo_request_timeout_seconds)
    ) as http_client:
        client = DodoApiClient(
            http_client,
            access_token=settings.dodo_access_token,
            country_id=settings.dodo_country_id,
            allowed_operations=settings.allowed_operations,
            max_retries=settings.dodo_max_retries,
        )
        base = {
            "countryId": settings.dodo_country_id,
            "businessId": settings.dodo_business_id,
        }

        async def fetch(page: dict[str, int]) -> dict:
            return await client.request_operation(operation, {**base, **page})

        units = await paginate(fetch, settings.endpoint_overrides["get-all-units"])
    settings.unit_catalog_path.parent.mkdir(parents=True, exist_ok=True)
    settings.unit_catalog_path.write_text(
        json.dumps({"units": units}, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"Сохранено заведений: {len(units)} → {settings.unit_catalog_path}")


if __name__ == "__main__":
    asyncio.run(run())
