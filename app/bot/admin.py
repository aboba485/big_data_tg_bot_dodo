from __future__ import annotations

import argparse

from app.config import get_settings
from app.storage.sqlite import SQLiteDatabase
from app.storage.users import TelegramUserRepository
from app.users.models import TelegramRole, TelegramUser


def main() -> None:
    parser = argparse.ArgumentParser(description="Управление доступом Telegram-бота")
    parser.add_argument("telegram_id", type=int)
    parser.add_argument("--role", choices=[item.value for item in TelegramRole], default="viewer")
    parser.add_argument("--reports", default="*")
    parser.add_argument("--units", default="*")
    parser.add_argument("--disable", action="store_true")
    args = parser.parse_args()

    settings = get_settings()
    database = SQLiteDatabase(settings.sqlite_path)
    database.initialize()
    repository = TelegramUserRepository(database)
    repository.upsert(
        TelegramUser(
            telegram_id=args.telegram_id,
            role=TelegramRole(args.role),
            allowed_report_types=_split(args.reports),
            allowed_unit_ids=_split(args.units),
            is_active=not args.disable,
        )
    )
    print(f"Пользователь {args.telegram_id} сохранён")


def _split(value: str) -> list[str]:
    return [part.strip() for part in value.split(",") if part.strip()]


if __name__ == "__main__":
    main()
