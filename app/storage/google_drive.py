from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from app.storage.sqlite import SQLiteDatabase


@dataclass(frozen=True)
class GoogleDriveLink:
    telegram_id: int
    email: str
    encrypted_tokens: str
    created_at: datetime
    updated_at: datetime


class GoogleDriveLinkRepository:
    def __init__(self, database: SQLiteDatabase) -> None:
        self.database = database

    def get(self, telegram_id: int) -> GoogleDriveLink | None:
        with self.database.connect() as connection:
            row = connection.execute(
                """
                SELECT telegram_id, email, encrypted_tokens, created_at, updated_at
                FROM google_drive_links
                WHERE telegram_id=?
                """,
                (telegram_id,),
            ).fetchone()
        if row is None:
            return None
        return GoogleDriveLink(
            telegram_id=int(row["telegram_id"]),
            email=str(row["email"]),
            encrypted_tokens=str(row["encrypted_tokens"]),
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )

    def upsert(
        self,
        telegram_id: int,
        email: str,
        encrypted_tokens: str,
        now_utc: datetime | None = None,
    ) -> None:
        stamp = (now_utc or datetime.now(UTC)).isoformat()
        with self.database.connect() as connection:
            connection.execute(
                """
                INSERT INTO google_drive_links(
                    telegram_id, email, encrypted_tokens, created_at, updated_at
                )
                VALUES(?,?,?,?,?)
                ON CONFLICT(telegram_id) DO UPDATE SET
                    email=excluded.email,
                    encrypted_tokens=excluded.encrypted_tokens,
                    updated_at=excluded.updated_at
                """,
                (telegram_id, email, encrypted_tokens, stamp, stamp),
            )

    def update_tokens(
        self, telegram_id: int, encrypted_tokens: str, now_utc: datetime | None = None
    ) -> bool:
        """Refresh stored tokens without recreating a link the user has just removed."""
        stamp = (now_utc or datetime.now(UTC)).isoformat()
        with self.database.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE google_drive_links
                SET encrypted_tokens=?, updated_at=?
                WHERE telegram_id=?
                """,
                (encrypted_tokens, stamp, telegram_id),
            )
        return cursor.rowcount == 1

    def delete(self, telegram_id: int) -> bool:
        with self.database.connect() as connection:
            cursor = connection.execute(
                "DELETE FROM google_drive_links WHERE telegram_id=?", (telegram_id,)
            )
        return cursor.rowcount == 1


class OAuthStateRepository:
    def __init__(self, database: SQLiteDatabase, ttl_seconds: int = 600) -> None:
        self.database = database
        self.ttl_seconds = ttl_seconds

    def create(self, state: str, telegram_id: int, now_utc: datetime | None = None) -> None:
        now = now_utc or datetime.now(UTC)
        with self.database.connect() as connection:
            # Every new link attempt prunes expired rows, so the table cannot grow unbounded.
            connection.execute(
                "DELETE FROM oauth_states WHERE created_at < ?",
                (self._expiry_threshold(now).isoformat(),),
            )
            connection.execute(
                "INSERT OR REPLACE INTO oauth_states(state, telegram_id, created_at) VALUES(?,?,?)",
                (state, telegram_id, now.isoformat()),
            )

    def consume(self, state: str, now_utc: datetime | None = None) -> int | None:
        """Delete the state unconditionally and return its owner only when still valid."""
        now = now_utc or datetime.now(UTC)
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT telegram_id, created_at FROM oauth_states WHERE state=?",
                (state,),
            ).fetchone()
            connection.execute("DELETE FROM oauth_states WHERE state=?", (state,))
        if row is None:
            return None
        if datetime.fromisoformat(row["created_at"]) < self._expiry_threshold(now):
            return None
        return int(row["telegram_id"])

    def _expiry_threshold(self, now: datetime) -> datetime:
        return now - timedelta(seconds=self.ttl_seconds)
