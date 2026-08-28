from __future__ import annotations

from dataclasses import dataclass

from app.config import PROJECT_ROOT, load_yaml
from app.reports.metric_registry import MetricRegistry
from app.users.models import TelegramUser


@dataclass(frozen=True)
class ReportCategory:
    category_id: str
    label: str
    metrics: tuple[str, ...]


class ReportCatalog:
    def __init__(self, metrics: MetricRegistry) -> None:
        configured = load_yaml(PROJECT_ROOT / "config/report_categories.yaml")
        known = set(metrics.all())
        categories: list[ReportCategory] = []
        covered: set[str] = set()
        for category_id, value in configured.items():
            category_metrics = tuple(str(item) for item in value.get("metrics", []))
            unknown = set(category_metrics) - known
            if unknown:
                raise ValueError(
                    f"Неизвестные метрики в категории {category_id}: {sorted(unknown)}"
                )
            covered.update(category_metrics)
            categories.append(
                ReportCategory(str(category_id), str(value["label"]), category_metrics)
            )
        missing = known - covered
        if missing:
            raise ValueError(f"Метрики без категории: {sorted(missing)}")
        self._categories = tuple(categories)
        self._aliases = metrics.aliases()

    def categories_for(self, user: TelegramUser) -> list[ReportCategory]:
        return [
            category
            for category in self._categories
            if any(user.can_access_report(metric) for metric in category.metrics)
        ]

    def category(self, category_id: str, user: TelegramUser) -> ReportCategory | None:
        return next(
            (item for item in self.categories_for(user) if item.category_id == category_id), None
        )

    def reports_for(self, category_id: str, user: TelegramUser) -> list[tuple[str, str]]:
        category = self.category(category_id, user)
        if category is None:
            return []
        return [
            (metric, self._aliases.get(metric, [metric])[0])
            for metric in category.metrics
            if user.can_access_report(metric)
        ]
