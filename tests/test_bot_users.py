from __future__ import annotations

import sqlite3

import pytest

from app.config import Settings
from app.storage.sqlite import SQLiteDatabase
from app.storage.users import TelegramUserRepository
from app.users.models import TelegramRole, TelegramUser
from app.users.service import UserAccessService


def test_allowed_and_unknown_telegram_users(settings: Settings) -> None:
    database = SQLiteDatabase(settings.sqlite_path)
    database.initialize()
    repository = TelegramUserRepository(database)
    access = UserAccessService(repository)
    access.bootstrap(
        settings.model_copy(update={"allowed_telegram_ids": [101], "admin_telegram_ids": [202]})
    )

    viewer = access.authorized(101)
    admin = access.authorized(202)
    assert viewer is not None and viewer.role == TelegramRole.VIEWER
    assert admin is not None and admin.role == TelegramRole.ADMIN
    assert access.authorized(303) is None


def test_public_user_gets_ephemeral_limited_access(settings: Settings) -> None:
    database = SQLiteDatabase(settings.sqlite_path)
    database.initialize()
    repository = TelegramUserRepository(database)
    access = UserAccessService(
        repository,
        public_access=True,
        public_report_types=["sales", "orders_count"],
        public_unit_ids=["unit-public"],
    )

    public_user = access.authorized(303)

    assert public_user is not None
    assert public_user.role == TelegramRole.VIEWER
    assert public_user.allowed_report_types == ["sales", "orders_count"]
    assert public_user.allowed_unit_ids == ["unit-public"]
    assert repository.get(303) is None


def test_stored_and_inactive_users_override_public_access(settings: Settings) -> None:
    database = SQLiteDatabase(settings.sqlite_path)
    database.initialize()
    repository = TelegramUserRepository(database)
    access = UserAccessService(
        repository,
        public_access=True,
        public_report_types=["sales"],
        public_unit_ids=["public-unit"],
    )
    repository.upsert(
        TelegramUser(
            telegram_id=401,
            role=TelegramRole.MANAGER,
            allowed_report_types=["orders_count"],
            allowed_unit_ids=["private-unit"],
        )
    )
    repository.upsert(
        TelegramUser(
            telegram_id=402,
            allowed_report_types=["*"],
            allowed_unit_ids=["*"],
            is_active=False,
        )
    )

    stored = access.authorized(401)
    assert stored is not None
    assert stored.allowed_report_types == ["orders_count"]
    assert stored.allowed_unit_ids == ["private-unit"]
    assert access.authorized(402) is None


def test_user_report_and_unit_permissions_are_enforced(settings: Settings) -> None:
    database = SQLiteDatabase(settings.sqlite_path)
    database.initialize()
    repository = TelegramUserRepository(database)
    user = TelegramUser(
        telegram_id=101,
        role=TelegramRole.MANAGER,
        allowed_report_types=["sales"],
        allowed_unit_ids=["unit-1"],
    )
    repository.upsert(user)
    loaded = repository.get(101)
    assert loaded == user

    UserAccessService.ensure_report_access(user, ["sales"])
    UserAccessService.ensure_unit_access(user, ["unit-1"])
    with pytest.raises(PermissionError, match="типу"):
        UserAccessService.ensure_report_access(user, ["orders_count"])
    with pytest.raises(PermissionError, match="подразделению"):
        UserAccessService.ensure_unit_access(user, ["unit-2"])


def test_user_repository_values_cannot_change_sql_structure(settings: Settings) -> None:
    database = SQLiteDatabase(settings.sqlite_path)
    database.initialize()
    repository = TelegramUserRepository(database)
    injection = "sales'); DROP TABLE telegram_users; --"
    repository.upsert(
        TelegramUser(
            telegram_id=404,
            allowed_report_types=[injection],
            allowed_unit_ids=["*"],
        )
    )

    assert repository.get(404).allowed_report_types == [injection]  # type: ignore[union-attr]
    with database.connect() as connection:
        table = connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            ("telegram_users",),
        ).fetchone()
    assert table is not None


def test_database_unavailable_is_not_silently_accepted(tmp_path) -> None:
    database = SQLiteDatabase(tmp_path)
    with pytest.raises(sqlite3.OperationalError):
        database.initialize()
