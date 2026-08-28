from __future__ import annotations

from datetime import UTC, datetime

from app.documentation.loader import DocumentationLoader
from app.documentation.models import EndpointCandidate, EndpointDocument
from app.documentation.parser import parse_operation, serialize_endpoint
from app.storage.sqlite import SQLiteDatabase


def build_candidate(endpoint: EndpointDocument, score: float) -> EndpointCandidate:
    return EndpointCandidate(
        operation_id=endpoint.operation_id,
        score=score,
        compact_summary=endpoint.compact_summary,
        response_fields=[field.path for field in endpoint.response_fields[:25]],
        parameters=endpoint.parameters,
    )


class DocumentationRepository:
    def __init__(
        self,
        database: SQLiteDatabase,
        loader: DocumentationLoader,
        *,
        allowed_operations: set[str],
        index_all_get: bool = True,
    ) -> None:
        self.database = database
        self.loader = loader
        self.allowed_operations = allowed_operations
        self.index_all_get = index_all_get

    def ensure_index(self, force: bool = False) -> bool:
        self.database.initialize()
        current_hash = self.loader.sha256()
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT value FROM documentation_meta WHERE key='documentation_hash'"
            ).fetchone()
            if not force and row and row["value"] == current_hash:
                count = connection.execute("SELECT COUNT(*) FROM endpoint_documents").fetchone()[0]
                if count:
                    return False
        bundle = self.loader.load()
        endpoints = []
        for operation in bundle.operations:
            is_allowed = operation.id in self.allowed_operations
            if operation.method.upper() != "GET" or operation.deprecated:
                continue
            if self.index_all_get or is_allowed:
                endpoints.append(parse_operation(operation))
        with self.database.connect() as connection:
            connection.execute("DELETE FROM endpoint_documents")
            connection.execute("DELETE FROM endpoint_fts")
            connection.executemany(
                "INSERT INTO endpoint_documents(operation_id, document_json, searchable_text)"
                " VALUES (?, ?, ?)",
                [
                    (item.operation_id, serialize_endpoint(item), item.searchable_text)
                    for item in endpoints
                ],
            )
            connection.executemany(
                "INSERT INTO endpoint_fts(operation_id, searchable_text) VALUES (?, ?)",
                [(item.operation_id, item.searchable_text) for item in endpoints],
            )
            values = {
                "documentation_hash": current_hash,
                "operation_count": str(bundle.operation_count),
                "get_operation_count": str(
                    sum(operation.method.upper() == "GET" for operation in bundle.operations)
                ),
                "indexed_operation_count": str(len(endpoints)),
                "last_rebuilt_at": datetime.now(UTC).isoformat(),
            }
            connection.executemany(
                "INSERT INTO documentation_meta(key,value) VALUES (?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                values.items(),
            )
        return True

    def get(self, operation_id: str) -> EndpointDocument | None:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT document_json FROM endpoint_documents WHERE operation_id=?",
                (operation_id,),
            ).fetchone()
        return EndpointDocument.model_validate_json(row["document_json"]) if row else None

    def list(self) -> list[EndpointDocument]:
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT document_json FROM endpoint_documents ORDER BY operation_id"
            ).fetchall()
        return [EndpointDocument.model_validate_json(row["document_json"]) for row in rows]

    def search_fts(self, expression: str, top_k: int) -> list[EndpointCandidate]:
        if not expression:
            return []
        with self.database.connect() as connection:
            try:
                rows = connection.execute(
                    "SELECT e.document_json, bm25(endpoint_fts) AS rank "
                    "FROM endpoint_fts JOIN endpoint_documents e USING(operation_id) "
                    "WHERE endpoint_fts MATCH ? ORDER BY rank LIMIT ?",
                    (expression, top_k),
                ).fetchall()
            except Exception:
                rows = []
        return [
            build_candidate(
                EndpointDocument.model_validate_json(row["document_json"]), float(-row["rank"])
            )
            for row in rows
        ]

    def search_like(self, tokens: list[str], top_k: int) -> list[EndpointCandidate]:
        if not tokens:
            return []
        endpoints = self.list()
        scored: list[tuple[float, EndpointDocument]] = []
        for endpoint in endpoints:
            text = endpoint.searchable_text.casefold().replace("ё", "е")
            score = sum(
                5 if token in endpoint.operation_id else 1 for token in tokens if token in text
            )
            if score:
                scored.append((float(score), endpoint))
        scored.sort(key=lambda item: (-item[0], item[1].operation_id))
        return [build_candidate(endpoint, score) for score, endpoint in scored[:top_k]]

    def stats(self) -> dict[str, str | int]:
        with self.database.connect() as connection:
            values = dict(connection.execute("SELECT key,value FROM documentation_meta").fetchall())
        return {
            "documentation_hash": values.get("documentation_hash", ""),
            "operation_count": int(values.get("operation_count", 0)),
            "get_operation_count": int(values.get("get_operation_count", 0)),
            "indexed_operation_count": int(values.get("indexed_operation_count", 0)),
            "allowed_operation_count": len(self.allowed_operations),
            "last_rebuilt_at": values.get("last_rebuilt_at", ""),
        }
