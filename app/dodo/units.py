from __future__ import annotations

import json
import re
from pathlib import Path

from pydantic import BaseModel

from app.errors import UnitResolutionError

UNIT_RE = re.compile(r"^(?:[0-9a-fA-F]{32}|[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12})$")
RU_TO_LATIN = str.maketrans(
    {
        "а": "a",
        "б": "b",
        "в": "v",
        "г": "g",
        "д": "d",
        "е": "e",
        "ё": "e",
        "ж": "zh",
        "з": "z",
        "и": "i",
        "й": "y",
        "к": "k",
        "л": "l",
        "м": "m",
        "н": "n",
        "о": "o",
        "п": "p",
        "р": "r",
        "с": "s",
        "т": "t",
        "у": "u",
        "ф": "f",
        "х": "kh",
        "ц": "ts",
        "ч": "ch",
        "ш": "sh",
        "щ": "sch",
        "ъ": "",
        "ы": "y",
        "ь": "",
        "э": "e",
        "ю": "yu",
        "я": "ya",
    }
)


def normalize_unit_name(value: str) -> str:
    normalized = value.casefold().strip().replace("ё", "е")
    normalized = re.sub(r"[‐‑‒–—−]", "-", normalized)
    return re.sub(r"\s+", " ", normalized)


def transliterate_unit_name(value: str) -> str:
    return normalize_unit_name(value).translate(RU_TO_LATIN)


class Unit(BaseModel):
    unit_id: str
    unit_name: str


class UnitResolution(BaseModel):
    status: str
    unit_ids: list[str] = []
    question: str | None = None
    options: list[dict[str, str]] = []


class UnitResolver:
    def __init__(
        self,
        catalog_path: Path,
        aliases_path: Path,
        default_unit_ids: list[str],
    ) -> None:
        self.catalog_path = catalog_path
        self.aliases_path = aliases_path
        self.default_unit_ids = default_unit_ids

    def _catalog(self) -> list[Unit]:
        if not self.catalog_path.exists():
            return []
        data = json.loads(self.catalog_path.read_text(encoding="utf-8"))
        rows = data.get("units", data) if isinstance(data, dict) else data
        result = []
        for row in rows:
            unit_id = row.get("id") or row.get("unitId") or row.get("uuid")
            name = row.get("name") or row.get("unitName") or ""
            if unit_id:
                result.append(Unit(unit_id=str(unit_id), unit_name=str(name)))
        return result

    def _aliases(self) -> dict[str, str]:
        if not self.aliases_path.exists():
            return {}
        data = json.loads(self.aliases_path.read_text(encoding="utf-8"))
        return {normalize_unit_name(str(key)): str(value) for key, value in data.items()}

    def resolve(self, references: list[str]) -> UnitResolution:
        if not references:
            if self.default_unit_ids:
                return UnitResolution(status="ready", unit_ids=self.default_unit_ids)
            return UnitResolution(
                status="needs_clarification", question="По каким заведениям нужен отчёт?"
            )
        catalog = self._catalog()
        if references == ["all"]:
            ids = [unit.unit_id for unit in catalog] or self.default_unit_ids
            if not ids:
                return UnitResolution(
                    status="needs_clarification",
                    question="Каталог заведений пуст. Укажите UUID заведений.",
                )
            return UnitResolution(status="ready", unit_ids=ids)
        aliases = self._aliases()
        resolved = []
        for reference in references:
            if UNIT_RE.fullmatch(reference):
                resolved.append(reference)
                continue
            normalized = normalize_unit_name(reference)
            if normalized in aliases:
                resolved.append(aliases[normalized])
                continue
            matches = [
                unit
                for unit in catalog
                if normalized in normalize_unit_name(unit.unit_name)
                or normalized in transliterate_unit_name(unit.unit_name)
            ]
            if len(matches) > 1:
                return UnitResolution(
                    status="needs_clarification",
                    question="Какое заведение вы имели в виду?",
                    options=[
                        {"unitId": item.unit_id, "unitName": item.unit_name} for item in matches
                    ],
                )
            if len(matches) == 1:
                resolved.append(matches[0].unit_id)
                continue
            raise UnitResolutionError(f"Заведение «{reference}» не найдено")
        return UnitResolution(status="ready", unit_ids=list(dict.fromkeys(resolved)))

    def recognize_in_query(self, query: str) -> list[Unit]:
        normalized_query = normalize_unit_name(query)
        catalog = self._catalog()
        by_id = {unit.unit_id: unit for unit in catalog}
        candidates = [(normalize_unit_name(unit.unit_name), unit.unit_id) for unit in catalog]

        # Add alias units to by_id so they can be recognized
        for alias_name, alias_id in self._aliases().items():
            if alias_id not in by_id:
                by_id[alias_id] = Unit(unit_id=alias_id, unit_name=alias_name)
            candidates.append((alias_name, alias_id))

        matches: list[tuple[int, int, Unit]] = []
        for name, unit_id in candidates:
            match = re.search(rf"(?<!\w){re.escape(name)}(?!\w)", normalized_query)
            unit = by_id.get(unit_id)
            if match and unit:
                matches.append((match.start(), -len(name), unit))
        matches.sort(key=lambda item: (item[0], item[1]))
        result = []
        seen = set()
        for _, _, unit in matches:
            if unit.unit_id not in seen:
                seen.add(unit.unit_id)
                result.append(unit)
        return result

    def names(self) -> dict[str, str]:
        result = {item.unit_id: item.unit_name for item in self._catalog()}
        for alias_name, unit_id in self._aliases().items():
            result.setdefault(unit_id, alias_name)
        return result
