"""Temporal extraction must not block the event loop.

Recall calls temporal extraction from an async request path. It is pure CPU, so
running it inline freezes the loop for its whole duration and stalls every other
in-flight request in the process. These tests pin both halves of the fix: the
async form returns identical results, and it keeps the loop scheduling.
"""

import asyncio
import time
from datetime import datetime

import pytest

from hindsight_api.engine.query_analyzer import DateparserQueryAnalyzer
from hindsight_api.engine.search.temporal_extraction import (
    _get_executor,
    extract_temporal_constraint,
    extract_temporal_constraint_async,
)
from tests.query_analyzer_corpus import REFERENCE_DATE, build_corpus, build_document_workload


@pytest.fixture(scope="module")
def analyzer():
    a = DateparserQueryAnalyzer()
    a.load()
    return a


async def test_async_matches_sync_on_whole_corpus(analyzer) -> None:
    """Offloading changes where the work runs, never what it returns."""
    for query, _category in build_corpus():
        expected = extract_temporal_constraint(query, reference_date=REFERENCE_DATE, analyzer=analyzer)
        actual = await extract_temporal_constraint_async(query, reference_date=REFERENCE_DATE, analyzer=analyzer)
        assert actual == expected, query


async def test_executor_is_single_worker() -> None:
    """One worker on purpose.

    The work is pure Python and holds the GIL, so extra workers add no
    parallelism — they just contend. Measured at 16 concurrent document-sized
    extractions, widening the pool cost throughput for nothing: 1 worker
    1438ms, 2 workers 2091ms, 4 workers 4751ms, unbounded 16688ms.
    """
    assert _get_executor()._max_workers == 1


async def test_concurrent_extractions_keep_the_loop_scheduling(analyzer) -> None:
    """The loop must keep getting ticks while extractions are in flight.

    Inline, the same workload gave the loop a single tick in ~1.3s. The bound
    here is deliberately loose (it is wall-clock on a possibly-loaded machine);
    it is checking for "the loop still runs", not a precise latency.
    """
    slow_text = build_document_workload()[2]  # ~1.8 KB, the worst case
    ticks = 0
    stop = asyncio.Event()

    async def heartbeat() -> None:
        nonlocal ticks
        while not stop.is_set():
            await asyncio.sleep(0.001)
            ticks += 1

    beat = asyncio.create_task(heartbeat())
    await asyncio.sleep(0.05)
    ticks = 0

    await asyncio.gather(
        *[
            extract_temporal_constraint_async(slow_text, reference_date=REFERENCE_DATE, analyzer=analyzer)
            for _ in range(8)
        ]
    )
    stop.set()
    await beat

    assert ticks > 20, f"event loop only got {ticks} ticks during 8 concurrent extractions (inline gives ~1)"


async def test_offloaded_errors_still_degrade_to_none(analyzer, monkeypatch) -> None:
    """A crash inside the worker thread must not escape as a failed recall."""

    def boom(*args, **kwargs):
        raise IndexError("list index out of range")

    monkeypatch.setattr(analyzer, "analyze", boom)
    assert (
        await extract_temporal_constraint_async("on the 3rd", reference_date=REFERENCE_DATE, analyzer=analyzer) is None
    )


async def test_async_form_is_not_slower_for_cheap_queries(analyzer) -> None:
    """Dispatch overhead must stay small relative to the fast path."""
    query = "how does the reranker work"
    for _ in range(5):
        await extract_temporal_constraint_async(query, reference_date=REFERENCE_DATE, analyzer=analyzer)

    start = time.perf_counter()
    for _ in range(50):
        await extract_temporal_constraint_async(query, reference_date=REFERENCE_DATE, analyzer=analyzer)
    per_call_ms = (time.perf_counter() - start) * 1000 / 50
    assert per_call_ms < 2.0, f"async fast path averaged {per_call_ms:.3f}ms/call"


async def test_module_level_patching_still_reaches_the_async_form(monkeypatch) -> None:
    """Existing tests patch the module attribute; that seam must keep working.

    ``test_recall_pipeline_toggles`` and ``test_temporal_recall_selection`` both
    monkeypatch ``temporal_extraction.extract_temporal_constraint`` to drive the
    retrieval path. Now that retrieval calls the async form, the patch only takes
    effect if the sync function is resolved at call time rather than captured.
    """
    import hindsight_api.engine.search.temporal_extraction as module

    sentinel = (datetime(2025, 1, 1), datetime(2025, 2, 1))
    monkeypatch.setattr(module, "extract_temporal_constraint", lambda *a, **k: sentinel)

    assert await extract_temporal_constraint_async("anything at all") == sentinel


async def test_reference_date_and_analyzer_are_forwarded(analyzer) -> None:
    """Arguments must survive the hop into the worker thread."""
    ref = datetime(2020, 5, 17, 8, 0, 0)
    result = await extract_temporal_constraint_async("yesterday", reference_date=ref, analyzer=analyzer)
    assert result is not None
    assert result[0].date() == datetime(2020, 5, 16).date()
