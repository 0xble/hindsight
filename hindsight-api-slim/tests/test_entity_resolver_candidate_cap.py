"""Candidate-set bounding in entity resolution (GH-3211).

Candidate volume per query text is whatever the fuzzy probe returns. On a bank
with many near-identical names that can be thousands of rows, and every one of
them costs a synchronous ``SequenceMatcher`` call on the event-loop thread — a
resolution batch then blocks the worker for minutes, so ``/health`` stops
answering and the orchestrator kills the worker mid-op.

These tests pin the two guarantees that prevent it:
  1. at most ``entity_resolution_max_candidates`` candidates are scored per mention;
  2. the scoring loop yields, so other tasks keep getting scheduled while it runs.
"""

import asyncio
import time
import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from hindsight_api.engine.memories.pg import entity_resolver as entity_resolver_module
from hindsight_api.engine.memories.pg.entity_resolver import EntityResolver


def _make_resolver(max_candidates: int = 200) -> EntityResolver:
    """Resolver whose entity INSERTs are stubbed (no DB), so only scoring runs."""
    pool = MagicMock()
    ops = MagicMock()
    ops.bulk_insert_entities = AsyncMock(
        side_effect=lambda conn, table, bank_id, names, dates, kinds: {n.lower(): uuid.uuid4() for n in names}
    )
    ops.fetch_missing_entity_ids = AsyncMock(return_value=[])
    pool.ops = ops
    return EntityResolver(
        pool=pool,
        entity_lookup="trigram",
        entity_resolution_max_candidates=max_candidates,
    )  # type: ignore[arg-type]


def _make_conn() -> MagicMock:
    conn = MagicMock()
    conn.backend_type = "postgresql"
    conn.fetch = AsyncMock(return_value=[])
    conn.fetchval = AsyncMock(return_value=True)
    conn.executemany = AsyncMock()
    return conn


def _candidates(count: int, name: str = "Acme Corporation") -> list[tuple]:
    """A polluted candidate set: many distinct names sharing trigrams with the query."""
    now = datetime.now(UTC)
    return [(uuid.uuid4(), f"{name} variant {i:05d}", None, now, 1) for i in range(count)]


@pytest.mark.asyncio
async def test_scoring_is_capped_at_max_candidates():
    """Only the top `max_candidates` per mention reach the scoring loop.

    Counted on the word-level check rather than on ``SequenceMatcher``: it runs once per
    candidate that reaches scoring, and nowhere else here (see below). ``SequenceMatcher`` is no longer
    one-per-candidate (the word-level check calls it too, and a candidate the trigram gate
    rejects never reaches the name score), and the trigram helpers are also used by the
    O(N^2) in-batch pass — so counting either measures the shape of the scoring code rather
    than the cap this test exists to pin.

    The in-batch pass runs the word-level check too, on the names this batch is about to create.
    So the four mentions are mutually dissimilar, each with its own candidate pool: no in-batch
    pair clears the 0.5 trigram bar to reach the check, and the count stays exactly the scoring
    loop's.
    """
    resolver = _make_resolver(max_candidates=50)
    entities_data = [
        {"text": name, "nearby_entities": []}
        for name in ("Acme Corporation", "Globex Industries", "Initech Systems", "Umbrella Holdings")
    ]
    all_candidates = {e["text"]: _candidates(1000, e["text"]) for e in entities_data}

    with patch(
        "hindsight_api.engine.memories.pg.entity_resolver._tokens_are_compatible",
        wraps=entity_resolver_module._tokens_are_compatible,
    ) as gate:
        await resolver._resolve_from_candidates(
            _make_conn(), "bank-1", entities_data, datetime.now(UTC), all_candidates, {}, None, None
        )

    # 4 mentions x 50 candidates — not 4 x 1000.
    assert gate.call_count == 4 * 50


@pytest.mark.asyncio
async def test_capping_keeps_the_exact_match():
    """Truncation must not drop the candidate that actually matches the mention."""
    resolver = _make_resolver(max_candidates=10)
    exact_id = uuid.uuid4()
    now = datetime.now(UTC)
    # The right answer sits at the very end of a large noisy candidate set.
    candidates = _candidates(500) + [(exact_id, "Acme Corporation", None, now, 1)]
    entities_data = [{"text": "Acme Corporation", "nearby_entities": []}]

    resolved = await resolver._resolve_from_candidates(
        _make_conn(),
        "bank-1",
        entities_data,
        now,
        {"Acme Corporation": candidates},
        {},
        None,
        None,
    )

    assert resolved[0].entity_id == str(exact_id)
    assert resolved[0].canonical_name == "Acme Corporation"


_WIDE_BATCH_MENTIONS = 20
_WIDE_BATCH_CANDIDATES = 2000


async def _longest_run_scored_without_a_turn(stall_on_first_score: float = 0.0) -> tuple[int, int]:
    """Score a wide batch beside a concurrent task; return (longest un-yielded run, total scored).

    Responsiveness is measured in event-loop turns, not seconds. A ticker task takes a
    turn every time the loop gets control back; every candidate the scoring loop scores
    is counted against the ticker's latest turn. The longest run of candidates scored with
    no turn in between is how long the loop was held — in units of work, so a pause from
    outside the loop (a GC sweep over a large test-worker heap, the runner descheduling an
    oversubscribed xdist worker) cannot inflate it, and a fast machine cannot hide a loop
    that never yields.

    ``stall_on_first_score`` blocks the thread for that long inside the first scored
    candidate, standing in for such an outside pause.
    """
    resolver = _make_resolver(max_candidates=_WIDE_BATCH_CANDIDATES)
    candidates = _candidates(_WIDE_BATCH_CANDIDATES)
    entities_data = [{"text": f"Acme Corporation {i}", "nearby_entities": []} for i in range(_WIDE_BATCH_MENTIONS)]
    all_candidates = {e["text"]: candidates for e in entities_data}

    turns = 0
    stop = False

    async def ticker() -> None:
        nonlocal turns
        while not stop:
            turns += 1
            await asyncio.sleep(0)

    # The scoring loop calls this exactly once per candidate it scores (the label skip
    # is the only thing ahead of it, and this batch has no labels).
    real_similarity = entity_resolver_module.trigram_set_similarity
    last_turn_seen = -1
    run = longest = scored = 0

    def counting_similarity(a: set[str], b: set[str]) -> float:
        nonlocal last_turn_seen, run, longest, scored
        if stall_on_first_score and scored == 0:
            time.sleep(stall_on_first_score)
        scored += 1
        if turns != last_turn_seen:
            last_turn_seen = turns
            run = 0
        run += 1
        longest = max(longest, run)
        return real_similarity(a, b)

    tick_task = asyncio.create_task(ticker())
    await asyncio.sleep(0)  # let the ticker start before scoring does
    with patch.object(entity_resolver_module, "trigram_set_similarity", counting_similarity):
        await resolver._resolve_from_candidates(
            _make_conn(), "bank-1", entities_data, datetime.now(UTC), all_candidates, {}, None, None
        )
    stop = True
    await tick_task
    return longest, scored


@pytest.mark.asyncio
async def test_scoring_loop_keeps_the_event_loop_responsive():
    """A wide resolution batch must not starve other tasks (e.g. the /health handler).

    20 mentions x 2000 candidates is 40k synchronous scorings; without the periodic
    yield (GH-3211) the concurrent task gets no turn until the whole batch is done.

    This used to assert on wall-clock gaps between ticks, which measured the machine
    rather than the loop both ways: on a loaded CI runner one 1.4s pause outside the loop
    failed a correctly yielding batch, and locally a loop that never yields finished in
    ~35ms, under the 0.5s floor, and passed.
    """
    longest, scored = await _longest_run_scored_without_a_turn()

    assert scored == _WIDE_BATCH_MENTIONS * _WIDE_BATCH_CANDIDATES
    assert longest <= entity_resolver_module._SCORING_YIELD_EVERY, (
        f"scored {longest} candidates without handing the event loop back "
        f"(yield interval is {entity_resolver_module._SCORING_YIELD_EVERY})"
    )


@pytest.mark.asyncio
async def test_responsiveness_check_catches_a_loop_that_never_yields(monkeypatch):
    """The check has teeth: with the yield effectively disabled, the whole batch is one run."""
    monkeypatch.setattr(entity_resolver_module, "_SCORING_YIELD_EVERY", 10**9)

    longest, scored = await _longest_run_scored_without_a_turn()

    assert longest == scored == _WIDE_BATCH_MENTIONS * _WIDE_BATCH_CANDIDATES


@pytest.mark.asyncio
async def test_responsiveness_check_is_not_failed_by_a_pause_outside_the_loop():
    """A long thread-wide stall mid-batch (the nightly failure's shape) does not count as holding the loop.

    The nightly that failed the wall-clock version saw ticks every ~5-12ms except one
    1.388s gap, in a 1.692s batch that takes ~40ms here: the process was paused, the
    loop was not starved. Injected here as a 0.6s block, past the old 0.5s bound.
    """
    longest, scored = await _longest_run_scored_without_a_turn(stall_on_first_score=0.6)

    assert scored == _WIDE_BATCH_MENTIONS * _WIDE_BATCH_CANDIDATES
    assert longest <= entity_resolver_module._SCORING_YIELD_EVERY


@pytest.mark.asyncio
async def test_trigram_query_caps_candidates_per_query_text():
    """The PG probe truncates in SQL, pre-ranked by real trigram similarity."""
    resolver = _make_resolver(max_candidates=42)
    conn = _make_conn()

    with patch.object(resolver, "_resolve_from_candidates", new=AsyncMock(return_value=[])):
        await resolver._resolve_entities_batch_trigram(
            conn=conn,
            bank_id="bank-1",
            entities_data=[{"text": "Alice", "nearby_entities": [], "event_date": None}],
            unit_event_date=None,
        )

    query = conn.fetch.call_args.args[0]
    assert "LATERAL" in query
    assert "LIMIT $3" in query
    assert "ORDER BY similarity(" in query
    assert conn.fetch.call_args.args[3] == 42


@pytest.mark.asyncio
async def test_oracle_query_caps_candidates_per_query_text():
    """The Oracle probe truncates per query text via ROW_NUMBER, ranked by Jaro-Winkler."""
    resolver = _make_resolver(max_candidates=42)
    resolver.entity_lookup = "oracle_fuzzy"
    conn = _make_conn()
    conn.backend_type = "oracle"

    with patch.object(resolver, "_resolve_from_candidates", new=AsyncMock(return_value=[])):
        await resolver._resolve_entities_batch_oracle_fuzzy(
            conn=conn,
            bank_id="bank-1",
            entities_data=[{"text": "Alice", "nearby_entities": [], "event_date": None}],
            unit_event_date=None,
        )

    query = conn.fetch.call_args.args[0]
    assert "ROW_NUMBER() OVER" in query
    assert "PARTITION BY q.query_text" in query
    assert "WHERE rn <= $3" in query
    assert conn.fetch.call_args.args[3] == 42


def test_max_candidates_must_be_positive():
    with pytest.raises(ValueError, match="entity_resolution_max_candidates must be >= 1"):
        EntityResolver(pool=None, entity_resolution_max_candidates=0)  # type: ignore[arg-type]
