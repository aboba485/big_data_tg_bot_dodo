from __future__ import annotations

import hashlib
import json
import zipfile
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from app.documentation.models import DocumentationBundle
from app.errors import DocumentationError


class DocumentationLoader:
    member_name = "Dodo_IS_API_Reference_Sorted.json"

    def __init__(self, zip_path: Path) -> None:
        self.zip_path = Path(zip_path)

    def sha256(self) -> str:
        if not self.zip_path.is_file():
            raise DocumentationError(
                f"Архив документации не найден. Ожидаемый путь: {self.zip_path}"
            )
        digest = hashlib.sha256()
        with self.zip_path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
        return digest.hexdigest()

    def load(self) -> DocumentationBundle:
        if not self.zip_path.is_file():
            raise DocumentationError(
                f"Архив документации не найден. Ожидаемый путь: {self.zip_path}"
            )
        try:
            with zipfile.ZipFile(self.zip_path) as archive:
                names = archive.namelist()
                match = next((name for name in names if Path(name).name == self.member_name), None)
                if not match:
                    raise DocumentationError(
                        f"В архиве отсутствует обязательный файл {self.member_name}"
                    )
                with archive.open(match) as stream:
                    payload: Any = json.load(stream)
        except DocumentationError:
            raise
        except zipfile.BadZipFile as exc:
            raise DocumentationError(
                "Архив документации повреждён", technical_message=str(exc)
            ) from exc
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise DocumentationError(
                "JSON документации повреждён", technical_message=str(exc)
            ) from exc
        try:
            return DocumentationBundle.model_validate(payload)
        except ValidationError as exc:
            raise DocumentationError(
                "Документация имеет неподдерживаемую структуру", technical_message=str(exc)
            ) from exc
