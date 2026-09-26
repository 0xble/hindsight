"""Fair scope-group selection for consolidation fetches.

The strict fetch takes the oldest unconsolidated facts in the bank. When one scope
group (typically the shared scope) holds most of the oldest facts, every fetch is
that group alone, its batches must run serially, and ``consolidation_llm_parallelism``
has nothing to run beside it.

``consolidation_fair_group_selection`` changes only *which* facts a fetch returns:
the oldest ``cap`` facts of each group, groups visited by their oldest fact. These
tests pin that, and pin that the invariants upstream relies on are untouched:

1. Off by default: the fetch is the strict global oldest-first fetch.
2. Within a group the order is still oldest-first.
3. Same-scope work never runs concurrently (the #1604 / #4063 invariant), even when
   the fair fetch hands the dispatcher many groups at once.
4. Disjoint groups do run concurrently once the fair fetch mixes them into one round.
"""

from __future__ import annotations

import asyncio
import json
import re
import uuid
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from hindsight_api.config import _get_raw_config
from hindsight_api.engine.consolidation import consolidator as consolidator_mod
from hindsight_api.engine.consolidation.consolidator import (
    _ConsolidationBatchResponse,
    _CreateAction,
    _fair_group_cap,
    _fetch_fair_unconsolidated_rows,
    _fetch_unconsolidated_rows,
    run_consolidation_job,
)
from hindsight_api.engine.memory_engine import MemoryEngine
from hindsight_api.engine.providers.mock_llm import MockLLM

_BASE = datetime(2026, 1, 1, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def enable_observations():
    config = _get_raw_config()
    original = config.enable_observations
    config.enable_observations = True
    yield
    config.enable_observations = original


def _override_config(memory: MemoryEngine, **overrides):
    raw = _get_raw_config()
    fake = type(raw)(**{**{f: getattr(raw, f) for f in raw.__dataclass_fields__}, **overrides})
    return patch.object(memory._config_resolver, "resolve_full_config", return_value=fake)


async def _insert(conn, bank_id: str, text: str, tags: list[str], scopes, minute: int) -> uuid.UUID:
    """Insert one experience fact with an explicit, ordered created_at."""
    mem_id = uuid.uuid4()
    await conn.execute(
        """
        INSERT INTO memory_units (id, bank_id, text, fact_type, tags, observation_scopes, created_at)
        VALUES ($1, $2, $3, 'experience', $4, $5::jsonb, $6)
        """,
        mem_id,
        bank_id,
        text,
        tags,
        json.dumps(scopes) if scopes is not None else None,
        _BASE + timedelta(minutes=minute),
    )
    return mem_id


def _mock_llm():
    mock_llm = MockLLM(provider="mock", api_key="", base_url="", model="mock-model")

    def callback(messages, scope):
        if scope != "consolidation":
            return _ConsolidationBatchResponse()
        prompt = "\n".join(m.get("content", "") for m in messages if m.get("role") == "user")
        fact_ids = re.findall(r"\[([0-9a-f-]{36})\]", prompt)
        return _ConsolidationBatchResponse(
            creates=[_CreateAction(text=f"Observation about fact {fid[:8]}", source_fact_ids=[fid]) for fid in fact_ids]
        )

    mock_llm.set_response_callback(callback)
    wrapper = MagicMock()
    wrapper.with_config.return_value = mock_llm
    return wrapper


async def _seed_skewed_backlog(conn, bank_id: str) -> dict[str, list[uuid.UUID]]:
    """A backlog shaped like the one this feature exists for.

    Ten shared-scope facts are the oldest in the bank, then three small tagged
    groups arrive after them. A strict fetch of 6 returns shared facts only.
    """
    ids: dict[str, list[uuid.UUID]] = defaultdict(list)
    minute = 0
    for i in range(10):
        ids["shared"].append(await _insert(conn, bank_id, f"Shared fact {i}", [f"session:s{i}"], "shared", minute))
        minute += 1
    for name in ("alice", "bob", "carol"):
        for i in range(3):
            ids[name].append(await _insert(conn, bank_id, f"{name} fact {i}", [f"user:{name}"], None, minute))
            minute += 1
    return ids


def test_fair_group_cap_is_one_lane_share():
    assert _fair_group_cap(1000, 16) == 63
    assert _fair_group_cap(1000, 1) == 1000
    assert _fair_group_cap(6, 4) == 2
    assert _fair_group_cap(1, 16) == 1
    assert _fair_group_cap(10, 0) == 10


def test_fair_group_selection_is_off_by_default():
    assert _get_raw_config().consolidation_fair_group_selection is False


@pytest.mark.asyncio
@pytest.mark.memory_backend_incompatible
async def test_strict_fetch_starves_tagged_groups_behind_shared(memory: MemoryEngine, request_context):
    """The problem, pinned: the strict fetch returns only the shared group."""
    bank_id = f"test-fair-strict-{uuid.uuid4().hex[:8]}"
    await memory.get_bank_profile(bank_id=bank_id, request_context=request_context)
    try:
        async with memory._pool.acquire() as conn:
            ids = await _seed_skewed_backlog(conn, bank_id)
            rows = await _fetch_unconsolidated_rows(conn, bank_id, ["experience", "world"], 6, None)
        assert [r["id"] for r in rows] == ids["shared"][:6]
    finally:
        await memory.delete_bank(bank_id, request_context=request_context)


@pytest.mark.asyncio
@pytest.mark.memory_backend_incompatible
async def test_fair_fetch_mixes_groups_and_keeps_group_order(memory: MemoryEngine, request_context):
    """Each group contributes at most ``cap`` facts, oldest first, groups by oldest fact."""
    bank_id = f"test-fair-mix-{uuid.uuid4().hex[:8]}"
    await memory.get_bank_profile(bank_id=bank_id, request_context=request_context)
    try:
        async with memory._pool.acquire() as conn:
            ids = await _seed_skewed_backlog(conn, bank_id)
            rows = await _fetch_fair_unconsolidated_rows(conn, bank_id, ["experience", "world"], 7, 2)
        assert rows is not None
        assert [r["id"] for r in rows] == [
            *ids["shared"][:2],
            *ids["alice"][:2],
            *ids["bob"][:2],
            ids["carol"][0],
        ]
        # Same row shape as the strict fetch, so the dispatcher needs no second path.
        async with memory._pool.acquire() as conn:
            strict = await _fetch_unconsolidated_rows(conn, bank_id, ["experience", "world"], 1, None)
        assert set(rows[0]) == set(strict[0])
    finally:
        await memory.delete_bank(bank_id, request_context=request_context)


@pytest.mark.asyncio
@pytest.mark.memory_backend_incompatible
async def test_fair_fetch_skips_consolidated_and_failed(memory: MemoryEngine, request_context):
    bank_id = f"test-fair-skip-{uuid.uuid4().hex[:8]}"
    await memory.get_bank_profile(bank_id=bank_id, request_context=request_context)
    try:
        async with memory._pool.acquire() as conn:
            done = await _insert(conn, bank_id, "done", ["user:a"], None, 0)
            failed = await _insert(conn, bank_id, "failed", ["user:a"], None, 1)
            live = await _insert(conn, bank_id, "live", ["user:a"], None, 2)
            await conn.execute("UPDATE memory_units SET consolidated_at = now() WHERE id = $1", done)
            await conn.execute("UPDATE memory_units SET consolidation_failed_at = now() WHERE id = $1", failed)
            rows = await _fetch_fair_unconsolidated_rows(conn, bank_id, ["experience", "world"], 10, 10)
        assert [r["id"] for r in rows] == [live]
    finally:
        await memory.delete_bank(bank_id, request_context=request_context)


@pytest.mark.asyncio
@pytest.mark.memory_backend_incompatible
async def test_fair_fetch_groups_by_the_dispatcher_key(memory: MemoryEngine, request_context):
    """Selection keys groups exactly as the dispatcher does.

    Facts with different native tags that all request ``shared`` are one group (one
    cap), and a combined fact with the same tags as a per_tag fact is a separate one.
    """
    bank_id = f"test-fair-key-{uuid.uuid4().hex[:8]}"
    await memory.get_bank_profile(bank_id=bank_id, request_context=request_context)
    try:
        async with memory._pool.acquire() as conn:
            s1 = await _insert(conn, bank_id, "s1", ["session:1"], "shared", 0)
            s2 = await _insert(conn, bank_id, "s2", ["session:2"], "shared", 1)
            await _insert(conn, bank_id, "s3", ["session:3"], [[]], 2)
            combined = await _insert(conn, bank_id, "c", ["a", "b"], None, 3)
            per_tag = await _insert(conn, bank_id, "p", ["a", "b"], "per_tag", 4)
            rows = await _fetch_fair_unconsolidated_rows(conn, bank_id, ["experience", "world"], 10, 2)
        assert [r["id"] for r in rows] == [s1, s2, combined, per_tag]
    finally:
        await memory.delete_bank(bank_id, request_context=request_context)


@pytest.mark.asyncio
@pytest.mark.memory_backend_incompatible
async def test_fair_selection_runs_groups_concurrently_without_same_scope_overlap(
    memory: MemoryEngine, request_context
):
    """End to end on the skewed backlog, with fair selection on.

    - every fact is consolidated, each into its own scope;
    - the first round already runs tagged groups beside the shared group;
    - no scope ever has two recalls in flight (the upstream invariant).
    """
    bank_id = f"test-fair-e2e-{uuid.uuid4().hex[:8]}"
    await memory.get_bank_profile(bank_id=bank_id, request_context=request_context)

    in_flight: dict[frozenset[str], int] = defaultdict(int)
    peak: dict[frozenset[str], int] = defaultdict(int)
    max_distinct = 0
    started: list[frozenset[str]] = []
    lock = asyncio.Lock()
    orig_find = consolidator_mod._find_related_observations

    async def tracked_find(*, memory_engine, bank_id, query, request_context, tags=None):
        nonlocal max_distinct
        scope = frozenset(tags or [])
        async with lock:
            started.append(scope)
            in_flight[scope] += 1
            peak[scope] = max(peak[scope], in_flight[scope])
            max_distinct = max(max_distinct, sum(1 for v in in_flight.values() if v))
        try:
            await asyncio.sleep(0.05)
            return await orig_find(
                memory_engine=memory_engine, bank_id=bank_id, query=query, request_context=request_context, tags=tags
            )
        finally:
            async with lock:
                in_flight[scope] -= 1

    try:
        async with memory._pool.acquire() as conn:
            await _seed_skewed_backlog(conn, bank_id)

        original_llm = memory._consolidation_llm_config
        memory._consolidation_llm_config = _mock_llm()
        try:
            with (
                _override_config(
                    memory,
                    consolidation_fair_group_selection=True,
                    consolidation_llm_parallelism=4,
                    consolidation_llm_batch_size=1,
                    consolidation_batch_size=8,
                    consolidation_max_memories_per_round=0,
                ),
                patch.object(memory, "submit_async_consolidation"),
                patch.object(consolidator_mod, "_find_related_observations", tracked_find),
            ):
                result = await run_consolidation_job(
                    memory_engine=memory, bank_id=bank_id, request_context=request_context
                )
        finally:
            memory._consolidation_llm_config = original_llm

        assert result["status"] == "completed"
        assert result["memories_processed"] == 19

        async with memory._pool.acquire() as conn:
            left = await conn.fetchval(
                "SELECT count(*) FROM memory_units WHERE bank_id = $1 AND fact_type = 'experience'"
                " AND consolidated_at IS NULL",
                bank_id,
            )
        assert left == 0

        for scope, n in peak.items():
            assert n <= 1, f"scope {set(scope) or '<shared>'} had {n} concurrent recalls"
        assert frozenset() in peak, "shared scope never ran"
        assert max_distinct >= 2, f"expected tagged groups beside shared, max concurrent scopes = {max_distinct}"
        # The regression this feature fixes: under the strict fetch the first round is
        # 8 shared facts, so a tagged group starts only after 8 serial shared calls.
        # With fair selection the first round carries tagged groups from the start.
        shared_before_first_tagged = started.index(next(s for s in started if s)) if any(started) else len(started)
        assert shared_before_first_tagged <= 2, (
            f"tagged groups waited behind {shared_before_first_tagged} shared calls; order = {started}"
        )

        obs = (
            await memory.list_memory_units(
                bank_id, fact_type="observation", limit=1000, request_context=request_context
            )
        )["items"]
        tag_sets = sorted(tuple(sorted(o["tags"] or [])) for o in obs)
        assert tag_sets.count(()) == 10
        for name in ("alice", "bob", "carol"):
            assert tag_sets.count((f"user:{name}",)) == 3
    finally:
        await memory.delete_bank(bank_id, request_context=request_context)


@pytest.mark.asyncio
@pytest.mark.memory_backend_incompatible
async def test_scoped_job_keeps_strict_fetch(memory: MemoryEngine, request_context):
    """A job restricted to observation scopes keeps its existing fetch even when on."""
    bank_id = f"test-fair-scoped-{uuid.uuid4().hex[:8]}"
    await memory.get_bank_profile(bank_id=bank_id, request_context=request_context)
    try:
        async with memory._pool.acquire() as conn:
            await _seed_skewed_backlog(conn, bank_id)
        original_llm = memory._consolidation_llm_config
        memory._consolidation_llm_config = _mock_llm()
        fair = MagicMock(side_effect=AssertionError("fair fetch must not run for a scoped job"))
        try:
            with (
                _override_config(memory, consolidation_fair_group_selection=True, consolidation_llm_batch_size=1),
                patch.object(memory, "submit_async_consolidation"),
                patch.object(consolidator_mod, "_fetch_fair_unconsolidated_rows", fair),
            ):
                result = await run_consolidation_job(
                    memory_engine=memory,
                    bank_id=bank_id,
                    request_context=request_context,
                    observation_scopes=[["user:alice"]],
                )
        finally:
            memory._consolidation_llm_config = original_llm
        assert result["status"] == "completed"
        assert result["memories_processed"] == 3
        fair.assert_not_called()
    finally:
        await memory.delete_bank(bank_id, request_context=request_context)
