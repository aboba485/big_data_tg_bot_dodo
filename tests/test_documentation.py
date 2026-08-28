from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from app.config import Settings
from app.documentation.loader import DocumentationLoader
from app.documentation.models import RawOperation
from app.documentation.parser import parse_operation
from app.documentation.repository import DocumentationRepository
from app.documentation.schema_flattener import flatten_schema
from app.errors import DocumentationError
from app.storage.sqlite import SQLiteDatabase


def test_default_unit_ids_are_csv(monkeypatch) -> None:
    monkeypatch.setenv("DEFAULT_UNIT_IDS", "first, second")
    assert Settings(_env_file=None).default_unit_ids == ["first", "second"]


def test_loader_reads_real_zip(settings) -> None:
    bundle = DocumentationLoader(settings.documentation_zip_path).load()
    assert bundle.operation_count == 119
    assert len(bundle.operations) == 119


def test_corrupted_zip(tmp_path: Path) -> None:
    path = tmp_path / "bad.zip"
    path.write_bytes(b"broken")
    with pytest.raises(DocumentationError, match="повреждён"):
        DocumentationLoader(path).load()


def test_missing_json_in_zip(tmp_path: Path) -> None:
    path = tmp_path / "empty.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("other.json", "{}")
    with pytest.raises(DocumentationError, match="отсутствует"):
        DocumentationLoader(path).load()


def test_missing_zip_has_expected_path(tmp_path: Path) -> None:
    path = tmp_path / "missing.zip"
    with pytest.raises(DocumentationError, match=str(path).replace("\\", "\\\\")):
        DocumentationLoader(path).load()


def test_flatten_nested_array_and_variants() -> None:
    schema = {
        "type": "object",
        "properties": {
            "result": {
                "type": "array",
                "items": {
                    "allOf": [
                        {"properties": {"date": {"type": "string", "format": "date"}}},
                        {"properties": {"value": {"oneOf": [{"type": "number"}]}}},
                    ]
                },
            }
        },
    }
    fields = {item.path for item in flatten_schema(schema)}
    assert {"result", "result[]", "result[].date", "result[].value"} <= fields


def test_flatten_cycle_is_bounded() -> None:
    schema: dict = {"type": "object", "properties": {}}
    schema["properties"]["self"] = schema
    assert len(flatten_schema(schema, max_depth=4)) < 10


def test_parse_operation() -> None:
    operation = RawOperation(
        id="example",
        method="get",
        path="/example",
        parameters=[{"name": "unit", "in": "query", "required": True}],
        responses={
            "200": {
                "content": {
                    "application/json": {"schema": {"properties": {"result": {"type": "number"}}}}
                }
            }
        },
    )
    endpoint = parse_operation(operation)
    assert endpoint.method == "GET"
    assert endpoint.response_fields[0].path == "result"


def test_repository_reuses_hash(settings) -> None:
    repository = DocumentationRepository(
        SQLiteDatabase(settings.sqlite_path),
        DocumentationLoader(settings.documentation_zip_path),
        allowed_operations=settings.allowed_operations,
    )
    assert repository.ensure_index() is True
    assert repository.ensure_index() is False
    assert repository.stats()["indexed_operation_count"] > 90
    assert repository.get("get-finances-sales-daily-units") is not None
