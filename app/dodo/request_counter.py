from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar


class RequestCounter:
    """Tracks both lifetime and report-scoped Dodo API request counts."""

    def __init__(self) -> None:
        self.request_count = 0
        self._scope: ContextVar[list[int] | None] = ContextVar(
            f"dodo_request_scope_{id(self)}",
            default=None,
        )

    def count_request(self) -> None:
        self.request_count += 1
        scoped_count = self._scope.get()
        if scoped_count is not None:
            scoped_count[0] += 1

    @contextmanager
    def request_scope(self) -> Iterator[list[int]]:
        scoped_count = [0]
        token = self._scope.set(scoped_count)
        try:
            yield scoped_count
        finally:
            self._scope.reset(token)
