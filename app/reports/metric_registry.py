from __future__ import annotations

import math
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, model_validator

from app.config import PROJECT_ROOT, load_yaml
from app.errors import ConfigurationError

SUPPORTED_AGGREGATIONS = {
    "average",
    "count",
    "count_unique",
    "duration_sum",
    "max",
    "min",
    "ratio_from_sums",
    "sum",
    "weighted_average",
}
SUPPORTED_GRANULARITIES = {"total", "hour", "day", "week", "month"}
SUPPORTED_GROUPS = {
    "unit",
    "hour",
    "day",
    "week",
    "month",
    "sales channel",
    "ingredient",
    "ingredient category",
    "stop reason",
}


class MetricDefinition(BaseModel):
    operation_id: str
    collection: str = ""
    nested_collection: str | None = None
    aggregation: str
    value_field: str | None = None
    numerator_field: str | None = None
    denominator_field: str | None = None
    weight_field: str | None = None
    multiplier: float = 1.0
    deduplication_field: str | None = None
    required_fields: list[str] = Field(default_factory=list)
    granularities: list[str] = Field(default_factory=list)
    groups: list[str] = Field(default_factory=list)
    value_kind: str | None = None

    @model_validator(mode="after")
    def validate_formula(self) -> MetricDefinition:
        if self.aggregation not in SUPPORTED_AGGREGATIONS:
            raise ValueError(f"неподдерживаемая агрегация: {self.aggregation}")
        if not self.required_fields:
            raise ValueError("required_fields не должен быть пустым")
        if not self.granularities or set(self.granularities) - SUPPORTED_GRANULARITIES:
            raise ValueError("указана неподдерживаемая гранулярность")
        if set(self.groups) - SUPPORTED_GROUPS:
            raise ValueError("указана неподдерживаемая группировка")
        if len(self.granularities) != len(set(self.granularities)):
            raise ValueError("гранулярности не должны повторяться")
        if len(self.groups) != len(set(self.groups)):
            raise ValueError("группировки не должны повторяться")
        required_pointers: tuple[str | None, ...]
        if self.aggregation in {"sum", "average", "count_unique", "min", "max"}:
            required_pointers = (self.value_field,)
        elif self.aggregation == "weighted_average":
            required_pointers = (self.value_field, self.weight_field)
        elif self.aggregation == "ratio_from_sums":
            required_pointers = (self.numerator_field, self.denominator_field)
        else:
            required_pointers = ()
        if any(not pointer for pointer in required_pointers):
            raise ValueError(f"не заполнены поля формулы {self.aggregation}")
        contract_pointers = (*required_pointers, self.deduplication_field)
        for pointer in (item for item in contract_pointers if item):
            if not any(
                field == pointer or field.endswith(f".{pointer}") for field in self.required_fields
            ):
                raise ValueError(f"поле формулы {pointer} отсутствует в required_fields")
        if self.aggregation == "duration_sum" and not {
            "id",
            "startedAtLocal",
            "endedAtLocal",
        } <= set(self.required_fields):
            raise ValueError("duration_sum требует id, startedAtLocal и endedAtLocal")
        if not math.isfinite(self.multiplier):
            raise ValueError("multiplier должен быть конечным числом")
        return self


class MetricRegistry:
    def __init__(self, config_directory: Path | None = None) -> None:
        root = config_directory or PROJECT_ROOT / "config"
        definitions: dict[str, Any] = load_yaml(root / "metrics.yaml")
        self._metrics = {
            metric_id: MetricDefinition.model_validate(value)
            for metric_id, value in definitions.items()
        }
        aliases: dict[str, list[str]] = load_yaml(root / "metric_aliases.yaml")
        self._aliases = {key: list(value) for key, value in aliases.items()}
        missing_aliases = set(self._metrics) - set(self._aliases)
        unknown_aliases = set(self._aliases) - set(self._metrics)
        blank_aliases = {
            metric_id
            for metric_id, values in self._aliases.items()
            if not values or any(not str(value).strip() for value in values)
        }
        if missing_aliases or unknown_aliases or blank_aliases:
            raise ConfigurationError(
                "Некорректный список aliases метрик",
                details={
                    "missing": sorted(missing_aliases),
                    "unknown": sorted(unknown_aliases),
                    "blank": sorted(blank_aliases),
                },
            )

    def get(self, metric_id: str) -> MetricDefinition:
        return self._metrics[metric_id]

    def has(self, metric_id: str) -> bool:
        return metric_id in self._metrics

    def all(self) -> dict[str, dict[str, Any]]:
        return {
            metric_id: {
                **definition.model_dump(),
                "aliases": self._aliases.get(metric_id, []),
            }
            for metric_id, definition in self._metrics.items()
        }

    def aliases(self) -> dict[str, list[str]]:
        return self._aliases

    def is_monetary(self, metric_id: str) -> bool:
        """Check if a metric represents monetary values."""
        if metric_id not in self._metrics:
            return False
        return self._metrics[metric_id].value_kind == "money"

    def match_aliases(self, normalized_query: str) -> list[str]:
        query_tokens = normalized_query.split()

        def matches(alias: str) -> bool:
            normalized_alias = alias.casefold().replace("ё", "е")
            if normalized_alias in normalized_query:
                return True
            alias_tokens = normalized_alias.split()
            return bool(alias_tokens) and all(
                any(
                    len(alias_token) >= 4
                    and len(query_token) >= 4
                    and (
                        query_token.startswith(alias_token[: min(6, len(alias_token))])
                        or alias_token.startswith(query_token[: min(6, len(query_token))])
                    )
                    for query_token in query_tokens
                )
                for alias_token in alias_tokens
            )

        return [
            metric_id
            for metric_id, aliases in self._aliases.items()
            if any(matches(alias) for alias in aliases)
        ]
