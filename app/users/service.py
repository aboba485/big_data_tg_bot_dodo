from __future__ import annotations

from app.config import Settings
from app.storage.users import TelegramUserRepository
from app.users.models import TelegramRole, TelegramUser


class UserAccessService:
    def __init__(
        self,
        repository: TelegramUserRepository,
        *,
        public_access: bool = False,
        public_report_types: list[str] | None = None,
        public_unit_ids: list[str] | None = None,
    ) -> None:
        self.repository = repository
        self.public_access = public_access
        self.public_report_types = public_report_types or []
        self.public_unit_ids = public_unit_ids or []

    def bootstrap(self, settings: Settings) -> None:
        for telegram_id in settings.allowed_telegram_ids:
            self.repository.create_if_missing(
                TelegramUser(
                    telegram_id=telegram_id,
                    role=TelegramRole.VIEWER,
                    allowed_report_types=["*"],
                    allowed_unit_ids=["*"],
                )
            )
        for telegram_id in settings.admin_telegram_ids:
            existing = self.repository.get(telegram_id)
            self.repository.upsert(
                TelegramUser(
                    telegram_id=telegram_id,
                    role=TelegramRole.ADMIN,
                    allowed_report_types=["*"],
                    allowed_unit_ids=["*"],
                    is_active=existing.is_active if existing else True,
                )
            )

    def authorized(self, telegram_id: int) -> TelegramUser | None:
        user = self.repository.get(telegram_id)
        if user is not None:
            return user if user.is_active else None
        if not self.public_access:
            return None
        return TelegramUser(
            telegram_id=telegram_id,
            role=TelegramRole.VIEWER,
            allowed_report_types=self.public_report_types,
            allowed_unit_ids=self.public_unit_ids,
        )

    @staticmethod
    def ensure_report_access(user: TelegramUser, report_types: list[str]) -> None:
        denied = [
            report_type for report_type in report_types if not user.can_access_report(report_type)
        ]
        if denied:
            raise PermissionError("Нет доступа к выбранному типу отчёта")

    @staticmethod
    def ensure_unit_access(user: TelegramUser, unit_ids: list[str]) -> None:
        denied = [unit_id for unit_id in unit_ids if not user.can_access_unit(unit_id)]
        if denied:
            raise PermissionError("Нет доступа к выбранному подразделению")
