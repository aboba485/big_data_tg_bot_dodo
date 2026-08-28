from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


class SQLiteDatabase:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    def initialize(self) -> None:
        with self.connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS documentation_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS endpoint_documents (
                    operation_id TEXT PRIMARY KEY,
                    document_json TEXT NOT NULL,
                    searchable_text TEXT NOT NULL
                );
                CREATE VIRTUAL TABLE IF NOT EXISTS endpoint_fts USING fts5(
                    operation_id UNINDEXED,
                    searchable_text,
                    tokenize='unicode61 remove_diacritics 2'
                );
                CREATE TABLE IF NOT EXISTS report_runs (
                    request_id TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL,
                    status TEXT NOT NULL,
                    query_text TEXT,
                    query_hash TEXT NOT NULL,
                    metric_ids TEXT NOT NULL DEFAULT '[]',
                    operation_ids TEXT NOT NULL DEFAULT '[]',
                    candidate_count INTEGER NOT NULL DEFAULT 0,
                    dodo_requests_count INTEGER NOT NULL DEFAULT 0,
                    openai_input_tokens INTEGER NOT NULL DEFAULT 0,
                    openai_output_tokens INTEGER NOT NULL DEFAULT 0,
                    duration_ms INTEGER NOT NULL DEFAULT 0,
                    error_code TEXT
                );
                CREATE TABLE IF NOT EXISTS generated_reports (
                    report_id TEXT PRIMARY KEY,
                    request_id TEXT NOT NULL,
                    path TEXT NOT NULL,
                    media_type TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(request_id) REFERENCES report_runs(request_id)
                );
                CREATE TABLE IF NOT EXISTS telegram_users (
                    telegram_id INTEGER PRIMARY KEY,
                    role TEXT NOT NULL CHECK(role IN ('admin', 'manager', 'viewer')),
                    allowed_report_types TEXT NOT NULL DEFAULT '[]',
                    allowed_unit_ids TEXT NOT NULL DEFAULT '[]',
                    is_active INTEGER NOT NULL DEFAULT 1 CHECK(is_active IN (0, 1)),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS weekly_report_subscriptions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    telegram_id INTEGER NOT NULL,
                    chat_id INTEGER NOT NULL,
                    metric_id TEXT NOT NULL,
                    unit_id TEXT NOT NULL,
                    output_format TEXT NOT NULL,
                    granularity TEXT NOT NULL DEFAULT 'total',
                    weekday INTEGER NOT NULL CHECK(weekday BETWEEN 0 AND 6),
                    local_hour INTEGER NOT NULL CHECK(local_hour BETWEEN 0 AND 23),
                    timezone TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0, 1)),
                    next_run_at TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS weekly_report_due_idx
                    ON weekly_report_subscriptions(enabled, next_run_at);
                CREATE TABLE IF NOT EXISTS google_drive_links (
                    telegram_id INTEGER PRIMARY KEY,
                    email TEXT NOT NULL,
                    encrypted_tokens TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS oauth_states (
                    state TEXT PRIMARY KEY,
                    telegram_id INTEGER NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS dodo_oauth_token (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    encrypted_payload TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS weekly_report_deliveries (
                    subscription_id INTEGER NOT NULL,
                    period_end TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('processing', 'sent', 'failed')),
                    claimed_at TEXT NOT NULL,
                    sent_at TEXT,
                    error_code TEXT,
                    PRIMARY KEY(subscription_id, period_end),
                    FOREIGN KEY(subscription_id) REFERENCES weekly_report_subscriptions(id)
                        ON DELETE CASCADE
                );
                """
            )
            columns = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(weekly_report_subscriptions)")
            }
            if "granularity" not in columns:
                connection.execute(
                    """ALTER TABLE weekly_report_subscriptions
                    ADD COLUMN granularity TEXT NOT NULL DEFAULT 'total'"""
                )
            if "spreadsheet_id" not in columns:
                connection.execute(
                    """ALTER TABLE weekly_report_subscriptions
                    ADD COLUMN spreadsheet_id TEXT"""
                )
            if "frequency" not in columns:
                connection.execute(
                    """ALTER TABLE weekly_report_subscriptions
                    ADD COLUMN frequency TEXT NOT NULL DEFAULT 'weekly'"""
                )
            if "day_of_month" not in columns:
                connection.execute(
                    """ALTER TABLE weekly_report_subscriptions
                    ADD COLUMN day_of_month INTEGER"""
                )

    def ping(self) -> bool:
        try:
            with self.connect() as connection:
                return connection.execute("SELECT 1").fetchone()[0] == 1
        except sqlite3.Error:
            return False
