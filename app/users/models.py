from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field


class TelegramRole(StrEnum):
    ADMIN = "admin"
    MANAGER = "manager"
    VIEWER = "viewer"


class TelegramUser(BaseModel):
    telegram_id: int
    role: TelegramRole = TelegramRole.VIEWER
    allowed_report_types: list[str] = Field(default_factory=list)
    allowed_unit_ids: list[str] = Field(default_factory=list)
    is_active: bool = True

    def can_access_report(self, report_type: str) -> bool:
        return (
            self.role == TelegramRole.ADMIN
            or "*" in self.allowed_report_types
            or (report_type in self.allowed_report_types)
        )

    def can_access_unit(self, unit_id: str) -> bool:
        return (
            self.role == TelegramRole.ADMIN
            or "*" in self.allowed_unit_ids
            or (unit_id in self.allowed_unit_ids)
        )
