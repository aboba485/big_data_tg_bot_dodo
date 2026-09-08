from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field


class BotPreparation(BaseModel):
    status: Literal["ready", "needs_clarification", "unsupported"]
    question: str | None = None
    reason: str | None = None
    report_types: list[str] = Field(default_factory=list)
    unit_ids: list[str] = Field(default_factory=list)
    date_from: date | None = None
    date_to: date | None = None
    sales_channel_options: list[str] = Field(default_factory=list)
    sales_channel_can_split: bool = False
    sales_channel_selection: str = ""


class BotReportResult(BaseModel):
    status: Literal["ready", "needs_clarification", "unsupported"]
    text: str = ""
    question: str | None = None
    reason: str | None = None
    report_id: str | None = None
    file_path: Path | None = None
    file_name: str | None = None
    sheet_url: str | None = None
    response: dict[str, Any] = Field(default_factory=dict)
