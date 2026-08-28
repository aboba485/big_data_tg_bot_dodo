from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from app.storage.sqlite import SQLiteDatabase


class GeneratedFileRepository:
    def __init__(self, database: SQLiteDatabase) -> None:
        self.database = database

    def add(self, report_id: str, request_id: str, path: Path, media_type: str) -> None:
        with self.database.connect() as connection:
            connection.execute(
                "INSERT INTO generated_reports(report_id,request_id,path,media_type,created_at)"
                " VALUES(?,?,?,?,?)",
                (
                    report_id,
                    request_id,
                    str(path.resolve()),
                    media_type,
                    datetime.now(UTC).isoformat(),
                ),
            )

    def get(self, report_id: str) -> tuple[Path, str] | None:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT path,media_type FROM generated_reports WHERE report_id=?",
                (report_id,),
            ).fetchone()
        return (Path(row["path"]), row["media_type"]) if row else None

    def delete(self, report_id: str) -> None:
        with self.database.connect() as connection:
            connection.execute("DELETE FROM generated_reports WHERE report_id=?", (report_id,))
