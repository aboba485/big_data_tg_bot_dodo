from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime

from cryptography.fernet import Fernet, InvalidToken

from app.storage.sqlite import SQLiteDatabase


@dataclass(frozen=True)
class DodoOAuthToken:
    access_token: str
    refresh_token: str
    expires_at: datetime
    updated_at: datetime


class DodoTokenRepository:
    SINGLETON_ID = 1

    def __init__(self, database: SQLiteDatabase, encryption_key: str) -> None:
        self.database = database
        self._cipher = Fernet(encryption_key.encode())

    def get(self) -> DodoOAuthToken | None:
        with self.database.connect() as connection:
            row = connection.execute(
                """
                SELECT encrypted_payload, expires_at, updated_at
                FROM dodo_oauth_token
                WHERE id = ?
                """,
                (self.SINGLETON_ID,),
            ).fetchone()
        if row is None:
            return None
        try:
            payload = json.loads(self._cipher.decrypt(row["encrypted_payload"].encode()).decode())
        except (InvalidToken, json.JSONDecodeError):
            return None
        return DodoOAuthToken(
            access_token=str(payload.get("access_token") or ""),
            refresh_token=str(payload.get("refresh_token") or ""),
            expires_at=datetime.fromisoformat(row["expires_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )

    def upsert(
        self,
        access_token: str,
        refresh_token: str,
        expires_at: datetime,
        now_utc: datetime | None = None,
    ) -> None:
        stamp = (now_utc or datetime.now(UTC)).isoformat()
        payload = json.dumps(
            {"access_token": access_token, "refresh_token": refresh_token},
            ensure_ascii=False,
        )
        encrypted = self._cipher.encrypt(payload.encode()).decode()
        with self.database.connect() as connection:
            connection.execute(
                """
                INSERT INTO dodo_oauth_token(id, encrypted_payload, expires_at, updated_at)
                VALUES(?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    encrypted_payload = excluded.encrypted_payload,
                    expires_at = excluded.expires_at,
                    updated_at = excluded.updated_at
                """,
                (self.SINGLETON_ID, encrypted, expires_at.isoformat(), stamp),
            )

    def delete(self) -> bool:
        with self.database.connect() as connection:
            cursor = connection.execute(
                "DELETE FROM dodo_oauth_token WHERE id = ?", (self.SINGLETON_ID,)
            )
        return cursor.rowcount == 1
