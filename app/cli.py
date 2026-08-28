from __future__ import annotations

import argparse
import json

from app.config import get_settings
from app.documentation import DocumentationLoader, DocumentationRepository
from app.google_drive.service import generate_encryption_key
from app.storage.sqlite import SQLiteDatabase


def repository() -> DocumentationRepository:
    settings = get_settings()
    return DocumentationRepository(
        SQLiteDatabase(settings.sqlite_path),
        DocumentationLoader(settings.documentation_zip_path),
        allowed_operations=settings.allowed_operations,
        index_all_get=settings.index_all_get_operations,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Dodo IS Natural Reports")
    parser.add_argument(
        "command",
        choices=["build-index", "inspect-docs", "list-operations", "google-key"],
    )
    arguments = parser.parse_args()
    if arguments.command == "google-key":
        print(generate_encryption_key())
        return
    repo = repository()
    if arguments.command == "build-index":
        rebuilt = repo.ensure_index(force=True)
        print(json.dumps({"rebuilt": rebuilt, **repo.stats()}, ensure_ascii=False, indent=2))
    elif arguments.command == "inspect-docs":
        repo.ensure_index()
        print(json.dumps(repo.stats(), ensure_ascii=False, indent=2))
    else:
        repo.ensure_index()
        for endpoint in repo.list():
            print(f"{endpoint.operation_id}\t{endpoint.method}\t{endpoint.path}")


if __name__ == "__main__":
    main()
