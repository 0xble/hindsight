"""Latency gates for temporal extraction.

Two kinds of assertion, deliberately:

* **CPU-time budgets** (default, fast) — ``time.process_time`` over the whole
  corpus. These are insensitive to machine load, so they are safe to run in CI
  and are the real regression guard.
* **Wall-clock p99 under concurrency** (``slow``) — the goal this work was set
  against: p99 < 20 ms with 32 simultaneous callers. Wall-clock on a loaded
  shared runner is noisy, so this is opt-in rather than part of fast CI.

Both budgets are generous multiples of the measured figures; they exist to catch
a return of the 100 ms behaviour, not to police small drift.
"""

import asyncio
import os
import time

import pytest

from tests.query_analyzer_bench import _default_fn, measure_burst, warmup
from tests.query_analyzer_corpus import (
    REFERENCE_DATE,
    build_corpus,
    build_document_workload,
    build_perf_workload,
    build_query_workload,
)

# Budgets. Measured on this branch: whole corpus ~0.55s CPU, non-temporal p50
# ~0.04ms. Set ~3x above measurement so ordinary variation does not flake.
CORPUS_CPU_BUDGET_S = float(os.getenv("HS_PERF_CORPUS_BUDGET", "2.0"))
NON_TEMPORAL_CPU_BUDGET_MS = float(os.getenv("HS_PERF_NON_TEMPORAL_BUDGET", "0.5"))
BURST_P99_BUDGET_MS = float(os.getenv("HS_PERF_BURST_P99_BUDGET", "20.0"))


@pytest.fixture(scope="module")
def analyze():
    fn = _default_fn()
    warmup(fn, build_perf_workload()[:60])
    return fn


def test_whole_corpus_cpu_budget(analyze) -> None:
    """Total CPU to analyse every corpus query stays within budget.

    Before this work the same sweep cost ~72 s of wall time; the guard here is
    against a regression of that magnitude, not against small drift.
    """
    corpus = build_corpus()
    start = time.process_time()
    for query, _category in corpus:
        analyze(query, REFERENCE_DATE)
    elapsed = time.process_time() - start
    assert elapsed < CORPUS_CPU_BUDGET_S, (
        f"corpus sweep took {elapsed:.2f}s CPU for {len(corpus)} queries (budget {CORPUS_CPU_BUDGET_S}s)"
    )


def test_non_temporal_queries_are_short_circuited(analyze) -> None:
    """The common recall case must not reach dateparser at all.

    A plain question with no date word and no digit is the single most frequent
    input recall sees, and it used to be the *slowest* (~60 ms) because every one
    of 205 locales ran to completion before concluding there was no date.
    """
    from hindsight_api.engine.query_analyzer import _query_can_score
    from tests.query_analyzer_corpus import NON_TEMPORAL_EN, TZ_ABBREV_TRAPS

    # Only queries the pre-filter actually rejects. Some entries in these lists
    # legitimately carry a digit (e.g. a URL with "?id=42") and must still take
    # the full search path, so they are not part of this claim.
    queries = [q for q in NON_TEMPORAL_EN + TZ_ABBREV_TRAPS if q.strip() and not _query_can_score(q)]
    assert len(queries) > 20, "expected the bulk of plain prose to be short-circuited"
    for _ in range(3):  # warm
        for q in queries:
            analyze(q, REFERENCE_DATE)

    worst = 0.0
    for q in queries:
        start = time.process_time()
        analyze(q, REFERENCE_DATE)
        worst = max(worst, (time.process_time() - start) * 1000)
    assert worst < NON_TEMPORAL_CPU_BUDGET_MS, f"slowest non-temporal query took {worst:.3f}ms CPU"


def test_period_fastpath_never_reaches_dateparser(analyze) -> None:
    """Regex-resolved period expressions must stay in the microsecond range."""
    from tests.query_analyzer_corpus import PERIOD_DE, PERIOD_EN, PERIOD_ES, PERIOD_FR, PERIOD_IT, PERIOD_RU

    queries = PERIOD_EN + PERIOD_ES + PERIOD_IT + PERIOD_FR + PERIOD_DE + PERIOD_RU
    for _ in range(3):
        for q in queries:
            analyze(q, REFERENCE_DATE)

    start = time.process_time()
    for q in queries:
        analyze(q, REFERENCE_DATE)
    per_query_ms = (time.process_time() - start) * 1000 / len(queries)
    assert per_query_ms < 0.1, f"period fast path averaged {per_query_ms:.4f}ms/query"


# Wall-clock percentiles cannot be measured while other xdist workers are
# saturating the CPU: the numbers reflect the runner's load, not this code. Skip
# rather than assert something meaningless (or, worse, flake).
requires_serial = pytest.mark.skipif(
    os.getenv("PYTEST_XDIST_WORKER") is not None,
    reason="wall-clock latency is not measurable under parallel test execution; run with -p no:xdist",
)


@requires_serial
@pytest.mark.slow
@pytest.mark.parametrize("concurrency", [32, 64, 128, 256])
def test_query_burst_p99_under_concurrency(analyze, concurrency: int) -> None:
    """p99 < 20 ms with N simultaneous callers on query-shaped input.

    Recall invokes temporal extraction inline in an ``async def``, so concurrent
    callers serialise behind each other on the event loop and a caller's latency
    includes that queueing. Wall-clock, hence ``slow``: a loaded runner inflates
    it and would flake fast CI.
    """
    stats = asyncio.run(measure_burst(analyze, build_query_workload(), concurrency))
    p99 = stats.pct(99)
    assert p99 < BURST_P99_BUDGET_MS, (
        f"burst@{concurrency} p99={p99:.2f}ms exceeds {BURST_P99_BUDGET_MS}ms "
        f"(p50={stats.pct(50):.2f} p95={stats.pct(95):.2f} max={stats.max:.2f})"
    )


@requires_serial
@pytest.mark.slow
def test_document_shaped_input_is_the_known_remaining_tail(analyze) -> None:
    """Document-shaped input still exceeds the budget, and that is known.

    Language detection is O(text length x 205 locales) and every locale runs to
    completion, so cost grows linearly with the input: ~230 characters is the
    point where a single call passes 20 ms. Queries are far below that; stored
    fact text (consolidation recalls) is not.

    This test documents the boundary rather than asserting a budget we do not
    meet. Getting past it means not running every locale, which needs its own
    equivalence proof; until then this pins where the cliff is.
    """
    stats = asyncio.run(measure_burst(analyze, build_document_workload(), 32))
    assert stats.pct(99) > BURST_P99_BUDGET_MS, (
        "document-shaped input now meets the budget -- delete this test and "
        "fold LONG_TEXTS back into the gated workload"
    )
