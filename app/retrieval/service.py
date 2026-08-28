from __future__ import annotations

from app.documentation.models import EndpointCandidate
from app.documentation.repository import DocumentationRepository, build_candidate
from app.reports.metric_registry import MetricRegistry
from app.retrieval.normalizer import fts_expression, normalize_query


class RetrievalService:
    def __init__(
        self,
        repository: DocumentationRepository,
        metrics: MetricRegistry,
        *,
        top_k: int = 6,
    ) -> None:
        self.repository = repository
        self.metrics = metrics
        self.top_k = top_k

    async def search_endpoints(
        self, query: str, top_k: int | None = None
    ) -> list[EndpointCandidate]:
        limit = top_k or self.top_k
        normalized = normalize_query(query)
        tokens = normalized.split()
        matched_metrics = self.metrics.match_aliases(normalized)
        priority_ids = list(
            dict.fromkeys(self.metrics.get(metric_id).operation_id for metric_id in matched_metrics)
        )
        found = self.repository.search_fts(fts_expression(tokens), limit * 2)
        if not found:
            found = self.repository.search_like(tokens, limit * 2)
        by_id = {candidate.operation_id: candidate for candidate in found}
        for operation_id in priority_ids:
            endpoint = self.repository.get(operation_id)
            if endpoint:
                by_id[operation_id] = build_candidate(endpoint, 1000.0)
        result = list(by_id.values())
        result.sort(key=lambda candidate: (-candidate.score, candidate.operation_id))
        return result[:limit]
