from __future__ import annotations

from pathlib import Path

import pytest

from app.config import PROJECT_ROOT, Settings

UNIT_ID = "000d3a240c719a8711e68aba13f7f862"


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,
        documentation_zip_path=PROJECT_ROOT / "Dodo_IS_API_Reference_Sorted.zip",
        sqlite_path=tmp_path / "app.db",
        reports_directory=tmp_path / "reports",
        unit_catalog_path=tmp_path / "units.json",
        unit_aliases_path=tmp_path / "aliases.json",
        default_unit_ids=[UNIT_ID],
        allowed_telegram_ids=[1, 2],
        dodo_mock_mode=True,
        planner_mock_mode=True,
    )
