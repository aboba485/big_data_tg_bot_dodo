from __future__ import annotations

import asyncio

import pytest

from app.dodo.request_counter import RequestCounter


@pytest.mark.asyncio
async def test_request_counts_are_isolated_between_parallel_reports() -> None:
    counter = RequestCounter()

    async def run_report(requests: int) -> int:
        with counter.request_scope() as scoped:
            await asyncio.gather(
                *(asyncio.to_thread(counter.count_request) for _ in range(requests))
            )
        return scoped[0]

    assert await asyncio.gather(run_report(2), run_report(3)) == [2, 3]
    assert counter.request_count == 5
