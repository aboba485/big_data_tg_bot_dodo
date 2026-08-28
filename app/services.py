from __future__ import annotations

from typing import Any

import httpx

from app.config import Settings
from app.documentation import DocumentationLoader, DocumentationRepository
from app.dodo.client import DodoApiClient
from app.dodo.executor import DodoExecutor
from app.dodo.mock import MockDodoApiClient
from app.dodo.oauth import DodoTokenRefresher
from app.dodo.profile import ProfileResolver
from app.dodo.units import UnitResolver
from app.google_drive.service import GoogleDriveService
from app.planner.service import PlannerService
from app.planner.validator import ReportPlanValidator
from app.report_service import ReportOrchestrator
from app.reports.metric_registry import MetricRegistry
from app.reports.service import ReportAggregator
from app.retrieval.service import RetrievalService
from app.storage.dodo_tokens import DodoTokenRepository
from app.storage.generated_files import GeneratedFileRepository
from app.storage.google_drive import GoogleDriveLinkRepository, OAuthStateRepository
from app.storage.report_runs import ReportRunRepository
from app.storage.sqlite import SQLiteDatabase
from app.storage.users import TelegramUserRepository
from app.storage.weekly_reports import WeeklyReportRepository
from app.users.service import UserAccessService


def build_services(settings: Settings, http_client: httpx.AsyncClient) -> dict[str, Any]:
    database = SQLiteDatabase(settings.sqlite_path)
    database.initialize()
    repository = DocumentationRepository(
        database,
        DocumentationLoader(settings.documentation_zip_path),
        allowed_operations=settings.allowed_operations,
        index_all_get=settings.index_all_get_operations,
    )
    repository.ensure_index()
    metrics = MetricRegistry()
    retrieval = RetrievalService(repository, metrics, top_k=settings.retrieval_top_k)
    resolver = UnitResolver(
        settings.unit_catalog_path, settings.unit_aliases_path, settings.default_unit_ids
    )
    planner = PlannerService(settings, metrics, unit_resolver=resolver)
    profiles = ProfileResolver(
        settings.endpoint_overrides,
        default_max_period_days=settings.raw_default_max_period_days,
    )
    validator = ReportPlanValidator(
        metrics,
        repository,
        settings.allowed_operations,
        allow_all_get=settings.allow_all_get_operations,
        profiles=profiles,
    )
    dodo_token_refresher: DodoTokenRefresher | None = None
    if settings.dodo_oauth_enabled:
        dodo_tokens = DodoTokenRepository(database, settings.dodo_token_encryption_key)
        dodo_token_refresher = DodoTokenRefresher(settings, dodo_tokens, http_client)

    client: Any
    if settings.dodo_mock_mode:
        client = MockDodoApiClient()
    elif dodo_token_refresher is not None:
        client = DodoApiClient(
            http_client,
            country_id=settings.dodo_country_id,
            allowed_operations=settings.allowed_operations,
            max_retries=settings.dodo_max_retries,
            allow_all_get=settings.allow_all_get_operations,
            token_provider=dodo_token_refresher.get_access_token,
        )
    else:
        client = DodoApiClient(
            http_client,
            access_token=settings.dodo_access_token,
            country_id=settings.dodo_country_id,
            allowed_operations=settings.allowed_operations,
            max_retries=settings.dodo_max_retries,
            allow_all_get=settings.allow_all_get_operations,
        )
    executor = DodoExecutor(
        client,
        repository,
        metrics,
        settings.endpoint_overrides,
        settings.allowed_operations,
        allow_all_get=settings.allow_all_get_operations,
        settings_values={
            "dodo_country_id": settings.dodo_country_id,
            "dodo_business_id": settings.dodo_business_id,
        },
        default_max_period_days=settings.raw_default_max_period_days,
        raw_max_rows=settings.raw_max_rows,
    )
    files = GeneratedFileRepository(database)
    orchestrator = ReportOrchestrator(
        settings,
        retrieval,
        planner,
        validator,
        resolver,
        executor,
        ReportAggregator(metrics, resolver.names()),
        ReportRunRepository(database, store_queries=settings.store_user_queries),
        files,
    )
    user_repository = TelegramUserRepository(database)
    weekly_reports = WeeklyReportRepository(database)
    public_unit_ids = (
        settings.telegram_public_unit_ids or settings.default_unit_ids or list(resolver.names())
    )
    user_access = UserAccessService(
        user_repository,
        public_access=settings.telegram_public_access,
        public_report_types=sorted(metrics.all()),
        public_unit_ids=public_unit_ids,
    )
    user_access.bootstrap(settings)
    google_drive = GoogleDriveService(
        settings,
        GoogleDriveLinkRepository(database),
        OAuthStateRepository(database, ttl_seconds=settings.google_oauth_state_ttl_seconds),
    )
    return {
        "database": database,
        "repository": repository,
        "metrics": metrics,
        "retrieval": retrieval,
        "planner": planner,
        "validator": validator,
        "resolver": resolver,
        "orchestrator": orchestrator,
        "files": files,
        "users": user_repository,
        "user_access": user_access,
        "weekly_reports": weekly_reports,
        "google_drive": google_drive,
        "dodo_token_refresher": dodo_token_refresher,
    }
