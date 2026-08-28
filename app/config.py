from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import urlparse

import yaml
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env", env_file_encoding="utf-8", extra="ignore"
    )

    app_env: str = "development"
    app_host: str = "0.0.0.0"
    app_port: int = 8000
    app_timezone: str = "Europe/Moscow"
    openai_api_key: str = ""
    openai_model: str = "gpt-5-mini"
    openai_max_output_tokens: int = 3000
    dodo_access_token: str = ""
    dodo_country_id: str = "ru"
    dodo_business_id: str = ""
    dodo_request_timeout_seconds: float = 40
    dodo_max_retries: int = 4
    dodo_oauth_client_id: str = ""
    dodo_oauth_client_secret: str = ""
    dodo_oauth_refresh_token: str = ""
    dodo_token_encryption_key: str = ""
    dodo_token_refresh_hour: int = Field(default=3, ge=0, le=23)
    dodo_token_refresh_minute: int = Field(default=5, ge=0, le=59)
    default_unit_ids: Annotated[list[str], NoDecode] = Field(default_factory=list)
    unit_catalog_path: Path = Path("data/units.json")
    unit_aliases_path: Path = Path("data/unit_aliases.json")
    documentation_zip_path: Path = Path("Dodo_IS_API_Reference_Sorted.zip")
    sqlite_path: Path = Path("data/app.db")
    reports_directory: Path = Path("data/reports")
    retrieval_top_k: int = 10
    index_all_get_operations: bool = True
    allow_all_get_operations: bool = True
    raw_default_max_period_days: int = 31
    raw_max_rows: int = 100_000
    llm_summary_enabled: bool = False
    dodo_mock_mode: bool = False
    planner_mock_mode: bool = False
    store_user_queries: bool = True
    log_level: str = "INFO"
    compatibility_api_key: str = ""
    telegram_bot_token: str = ""
    telegram_public_access: bool = True
    telegram_public_unit_ids: Annotated[list[str], NoDecode] = Field(default_factory=list)
    allowed_telegram_ids: Annotated[list[int], NoDecode] = Field(default_factory=list)
    admin_telegram_ids: Annotated[list[int], NoDecode] = Field(default_factory=list)
    telegram_private_chats_only: bool = True
    telegram_rate_limit_per_minute: int = Field(default=10, ge=1, le=120)
    telegram_max_concurrent_reports_per_user: int = Field(default=1, ge=1, le=5)
    telegram_report_timeout_seconds: float = Field(default=120, gt=0, le=900)
    telegram_max_report_days: int = Field(default=366, ge=1, le=3660)
    telegram_max_message_rows: int = Field(default=20, ge=1, le=100)
    telegram_fsm_ttl_seconds: int = Field(default=1800, ge=60, le=86400)
    telegram_scheduler_poll_seconds: int = Field(default=30, ge=5, le=3600)
    telegram_max_weekly_reports_per_user: int = Field(default=10, ge=1, le=50)
    google_client_id: str = ""
    google_client_secret: str = ""
    google_redirect_uri: str = ""
    google_token_encryption_key: str = ""
    google_oauth_state_ttl_seconds: int = Field(default=600, ge=60, le=3600)
    google_sheets_max_rows: int = Field(default=20_000, ge=1, le=1_000_000)

    @field_validator("default_unit_ids", "telegram_public_unit_ids", mode="before")
    @classmethod
    def split_unit_ids(cls, value: Any) -> Any:
        if isinstance(value, str):
            return [part.strip() for part in value.split(",") if part.strip()]
        return value

    @field_validator("allowed_telegram_ids", "admin_telegram_ids", mode="before")
    @classmethod
    def split_telegram_ids(cls, value: Any) -> Any:
        if isinstance(value, str):
            return [int(part.strip()) for part in value.split(",") if part.strip()]
        return value

    @field_validator("google_redirect_uri", mode="after")
    @classmethod
    def check_google_redirect_uri(cls, value: str) -> str:
        if not value:
            return value
        parsed = urlparse(value)
        localhost = parsed.hostname in {"localhost", "127.0.0.1", "::1"}
        if parsed.scheme != "https" and not (parsed.scheme == "http" and localhost):
            raise ValueError("GOOGLE_REDIRECT_URI должен использовать HTTPS")
        return value

    @field_validator(
        "documentation_zip_path",
        "sqlite_path",
        "reports_directory",
        "unit_catalog_path",
        "unit_aliases_path",
        mode="after",
    )
    @classmethod
    def resolve_path(cls, value: Path) -> Path:
        return value if value.is_absolute() else PROJECT_ROOT / value

    @property
    def google_drive_enabled(self) -> bool:
        return bool(
            self.google_client_id
            and self.google_client_secret
            and self.google_redirect_uri
            and self.google_token_encryption_key
        )

    @property
    def dodo_oauth_enabled(self) -> bool:
        return bool(
            self.dodo_oauth_client_id
            and self.dodo_oauth_client_secret
            and self.dodo_token_encryption_key
        )

    @property
    def allowed_operations(self) -> set[str]:
        data = load_yaml(PROJECT_ROOT / "config/allowed_operations.yaml")
        return set(data["operations"])

    @property
    def endpoint_overrides(self) -> dict[str, dict[str, Any]]:
        return load_yaml(PROJECT_ROOT / "config/endpoint_overrides.yaml")


def load_yaml(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as stream:
        value = yaml.safe_load(stream)
    if not isinstance(value, dict):
        raise ValueError(f"Ожидался объект в конфигурации {path}")
    return value


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


@lru_cache
def get_settings() -> Settings:
    return Settings()
