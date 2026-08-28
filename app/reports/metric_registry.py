from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from app.config import PROJECT_ROOT, load_yaml


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
