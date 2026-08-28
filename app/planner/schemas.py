from __future__ import annotations

from datetime import date
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class PlanStatus(StrEnum):
    READY = "ready"
    NEEDS_CLARIFICATION = "needs_clarification"
    UNSUPPORTED = "unsupported"


class Granularity(StrEnum):
    TOTAL = "total"
    HOUR = "hour"
    DAY = "day"
    WEEK = "week"
    MONTH = "month"


class OutputFormat(StrEnum):
    TABLE = "table"
    JSON = "json"
    CSV = "csv"
    XLSX = "xlsx"
    SHEETS = "sheets"


class PlanMode(StrEnum):
    METRICS = "metrics"
    DYNAMIC = "dynamic"
    RAW = "raw"


class AggregationType(StrEnum):
    SUM = "sum"
    COUNT = "count"
    COUNT_UNIQUE = "count_unique"
    AVERAGE = "average"
    WEIGHTED_AVERAGE = "weighted_average"
    RATIO = "ratio"
    MIN = "min"
    MAX = "max"


class DynamicAggregation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    value_field: str
    aggregation: AggregationType
    weight_field: str | None = None
    numerator_field: str | None = None
    denominator_field: str | None = None
    collection: str = ""


class ReportFilter(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: Literal["salesChannel", "orderSource", "paymentMethod"]
    values: list[str] = Field(default_factory=list)


class OperationArgument(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    value: str
    location: Literal["query", "path"] = "query"


class ReportPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: PlanStatus
    mode: PlanMode = PlanMode.METRICS
    clarification_question: str | None = None
    unsupported_reason: str | None = None
    metric_ids: list[str] = Field(default_factory=list)
    operation_ids: list[str] = Field(default_factory=list)
    dynamic_aggregation: DynamicAggregation | None = None
    operation_arguments: list[OperationArgument] = Field(default_factory=list)
    raw_collection: str = ""
    date_from: date | None = None
    date_to: date | None = None
    unit_references: list[str] = Field(default_factory=list)
    granularity: Granularity = Granularity.TOTAL
    group_by: list[str] = Field(default_factory=list)
    filters: list[ReportFilter] = Field(default_factory=list)
    output_format: OutputFormat = OutputFormat.TABLE
    selected_response_fields: list[str] = Field(default_factory=list)
    explanation: str = ""


class PlannerUsage(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0


class PlannerResult(BaseModel):
    plan: ReportPlan
    usage: PlannerUsage = Field(default_factory=PlannerUsage)
