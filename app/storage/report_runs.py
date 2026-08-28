from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any

from app.storage.sqlite import SQLiteDatabase


class ReportRunRepository:
    def __init__(self, database: SQLiteDatabase, *, store_queries: bool = True) -> None:
        self.database = database
        self.store_queries = store_queries

    def create(self, request_id: str, query: str) -> None:
        query_hash = hashlib.sha256(query.encode("utf-8")).hexdigest()
        with self.database.connect() as connection:
            connection.execute(
                "INSERT INTO report_runs(request_id,created_at,status,query_text,query_hash)"
                " VALUES(?,?,?,?,?)",
                (
                    request_id,
                    datetime.now(UTC).isoformat(),
                    "running",
                    query if self.store_queries else None,
                    query_hash,
                ),
            )

    def finish(self, request_id: str, **values: Any) -> None:
        allowed = {
            "status",
            "metric_ids",
            "operation_ids",
            "candidate_count",
            "dodo_requests_count",
            "openai_input_tokens",
            "openai_output_tokens",
            "duration_ms",
            "error_code",
        }
        items = {key: value for key, value in values.items() if key in allowed}
        for key in ("metric_ids", "operation_ids"):
            if key in items:
                items[key] = json.dumps(items[key], ensure_ascii=False)
        if not items:
            return
        assignment = ", ".join(f"{key}=?" for key in items)
        with self.database.connect() as connection:
            connection.execute(
                f"UPDATE report_runs SET {assignment} WHERE request_id=?",
                (*items.values(), request_id),
            )
