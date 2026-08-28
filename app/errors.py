from __future__ import annotations

from typing import Any


class ApplicationError(Exception):
    code = "application_error"
    status_code = 500
    public_message = "Не удалось выполнить запрос"

    def __init__(
        self,
        message: str | None = None,
        *,
        details: dict[str, Any] | None = None,
        technical_message: str | None = None,
    ) -> None:
        super().__init__(message or self.public_message)
        self.message = message or self.public_message
        self.details = details or {}
        self.technical_message = technical_message


class ConfigurationError(ApplicationError):
    code = "configuration_error"


class DocumentationError(ApplicationError):
    code = "documentation_error"


class RetrievalError(ApplicationError):
    code = "retrieval_error"


class PlannerError(ApplicationError):
    code = "planner_error"
    status_code = 422


class PlanValidationError(ApplicationError):
    code = "plan_validation_error"
    status_code = 422


class UnitResolutionError(ApplicationError):
    code = "unit_resolution_error"
    status_code = 422


class DodoApiError(ApplicationError):
    code = "dodo_api_error"
    status_code = 502


class DodoUnauthorizedError(DodoApiError):
    code = "dodo_api_unauthorized"
    status_code = 401
    public_message = "Dodo IS отклонил токен доступа"


class DodoForbiddenError(DodoApiError):
    code = "dodo_api_forbidden"
    status_code = 403
    public_message = "У токена недостаточно прав для получения этой метрики"


class DodoRateLimitError(DodoApiError):
    code = "dodo_api_rate_limit"
    status_code = 429
    public_message = "Dodo IS временно ограничил частоту запросов"


class DodoValidationError(DodoApiError):
    code = "dodo_api_validation"
    status_code = 422


class DodoTokenRefreshError(DodoApiError):
    code = "dodo_token_refresh_error"
    status_code = 502
    public_message = "Не удалось обновить токен доступа Dodo IS"


class ReportAggregationError(ApplicationError):
    code = "report_aggregation_error"


class ExportError(ApplicationError):
    code = "export_error"


class GoogleDriveError(ApplicationError):
    code = "google_drive_error"
    status_code = 502
    public_message = "Не удалось обратиться к Google Drive"


class GoogleDriveNotLinkedError(GoogleDriveError):
    code = "google_drive_not_linked"
    status_code = 400
    public_message = "Google Drive не подключён"
