from __future__ import annotations

import json
from datetime import UTC, datetime

from app.storage.sqlite import SQLiteDatabase
from app.users.models import TelegramRole, TelegramUser


class TelegramUserRepository:
    def __init__(self, database: SQLiteDatabase) -> None:
        self.database = database

    def get(self, telegram_id: int) -> TelegramUser | None:
        with self.database.connect() as connection:
            row = connection.execute(
                """
                SELECT telegram_id, role, allowed_report_types, allowed_unit_ids, is_active
                FROM telegram_users
                WHERE telegram_id=?
                """,
                (telegram_id,),
            ).fetchone()
        if row is None:
            return None
        return TelegramUser(
            telegram_id=row["telegram_id"],
            role=TelegramRole(row["role"]),
            allowed_report_types=json.loads(row["allowed_report_types"]),
            allowed_unit_ids=json.loads(row["allowed_unit_ids"]),
            is_active=bool(row["is_active"]),
        )

    def upsert(self, user: TelegramUser) -> None:
        now = datetime.now(UTC).isoformat()
        with self.database.connect() as connection:
            connection.execute(
                """
                INSERT INTO telegram_users(
                    telegram_id, role, allowed_report_types, allowed_unit_ids,
                    is_active, created_at, updated_at
                )
                VALUES(?,?,?,?,?,?,?)
                ON CONFLICT(telegram_id) DO UPDATE SET
                    role=excluded.role,
                    allowed_report_types=excluded.allowed_report_types,
                    allowed_unit_ids=excluded.allowed_unit_ids,
                    is_active=excluded.is_active,
                    updated_at=excluded.updated_at
                """,
                (
                    user.telegram_id,
                    user.role.value,
                    json.dumps(user.allowed_report_types, ensure_ascii=False),
                    json.dumps(user.allowed_unit_ids, ensure_ascii=False),
                    int(user.is_active),
                    now,
                    now,
                ),
            )

    def create_if_missing(self, user: TelegramUser) -> TelegramUser:
        existing = self.get(user.telegram_id)
        if existing is not None:
            return existing
        self.upsert(user)
        return user
