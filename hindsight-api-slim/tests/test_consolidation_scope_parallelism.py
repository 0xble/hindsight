"""End-to-end tests for parallel consolidation under each ``observation_scopes`` mode.

These tests pin two invariants the per-scope lock design has to enforce:

1. **Scope correctness**: regardless of ``consolidation_llm_parallelism``, each
   ``observation_scopes`` mode produces observations whose tags match the
   scope spec — combined writes to the memory's exact tag set, per_tag writes
   one per tag, all_combinations writes one per nonempty subset, and an
   explicit list writes one per declared scope. This is what the recall path
   was given, and what the write path persisted.
2. **No concurrent in-flight LLM call on a shared scope**: the per-scope lock
   guarantee. We wrap ``_find_related_observations`` to record entry/exit
   per scope tag set and assert max concurrency == 1 per scope, even under
   parallelism > 1 with deliberately overlapping write scopes.

All tests use the mock-LLM ``memory`` fixture (pool_max_size=5; well above the
parallelism levels used here).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time
import uuid
from collections import defaultdict
from unittest.mock import MagicMock, patch

import pytest

from hindsight_api.config import _get_raw_config
from hindsight_api.engine.consolidation.consolidator import (
    _ConsolidationBatchResponse,
    _CreateAction,
    _UpdateAction,
    _effective_lane_parallelism,
    run_consolidation_job,
)
from hindsight_api.engine.memory_engine import MemoryEngine
from hindsight_api.engine.providers.mock_llm import MockLLM
from hindsight_api.engine.response_models import MemoryFact, RecallResult

# ---------------------------------------------------------------------------
# Fixtures + helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def enable_observations():
    config = _get_raw_config()
    original = config.enable_observations
    config.enable_observations = True
    yield
    config.enable_observations = original


def _override_config(memory: MemoryEngine, **overrides):
    """Patch the resolver to return a fake config for this test only.

    Returns a context-manager-friendly patcher. Usage::

        with _override_config(memory, consolidation_llm_parallelism=4, ...):
            await run_consolidation_job(...)
    """
    raw = _get_raw_config()
    fake = type(raw)(
        **{
            **{f: getattr(raw, f) for f in raw.__dataclass_fields__},
            **overrides,
        }
    )
    return patch.object(memory._config_resolver, "resolve_full_config", return_value=fake)


async def _insert_memory(
    conn,
    bank_id: str,
    text: str,
    tags: list[str],
    observation_scopes,
) -> uuid.UUID:
    """Insert a single experience memory with an explicit observation_scopes column.

    The JSONB column is written as a JSON-encoded string to match how the API
    write path stores per-memory scope overrides.
    """
    mem_id = uuid.uuid4()
    await conn.execute(
        """
        INSERT INTO memory_units (id, bank_id, text, fact_type, tags, observation_scopes, created_at)
        VALUES ($1, $2, $3, 'experience', $4, $5::jsonb, now())
        """,
        mem_id,
        bank_id,
        text,
        tags,
        json.dumps(observation_scopes) if observation_scopes is not None else None,
    )
    return mem_id


def _mock_llm_one_obs_per_fact():
    """A MockLLM wrapper that emits one CREATE per fact in the prompt.

    Returned as (config_wrapper, mock_llm). The config wrapper short-circuits
    ``.with_config(...)`` to return the underlying MockLLM unchanged so we
    don't have to mock the whole per-bank config plumbing.
    """
    mock_llm = MockLLM(provider="mock", api_key="", base_url="", model="mock-model")

    def callback(messages, scope):
        if scope != "consolidation":
            return _ConsolidationBatchResponse()
        # Facts live in the user message; the system message (stable, cached) carries
        # example UUIDs in its OUTPUT samples — read user only.
        prompt = "\n".join(m.get("content", "") for m in messages if m.get("role") == "user")
        fact_ids = re.findall(r"\[([0-9a-f-]{36})\]", prompt)
        creates = [_CreateAction(text=f"Observation about fact {fid[:8]}", source_fact_ids=[fid]) for fid in fact_ids]
        return _ConsolidationBatchResponse(creates=creates)

    mock_llm.set_response_callback(callback)
    wrapper = MagicMock()
    wrapper.with_config.return_value = mock_llm
    return wrapper, mock_llm


async def _fetch_observation_tag_sets(memory: MemoryEngine, bank_id: str, request_context) -> list[frozenset[str]]:
    """Return the tag set (as a frozenset) of every observation in the bank."""
    items = (
        await memory.list_memory_units(bank_id, fact_type="observation", limit=1000, request_context=request_context)
    )["items"]
    return [frozenset(i["tags"] or []) for i in items]


# ---------------------------------------------------------------------------
# Scope-correctness tests: observations land at the right scopes under
# parallelism > 1, for each observation_scopes mode.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.memory_backend_incompatible
async def test_combined_mode_parallel_writes_to_memory_tag_set(memory: MemoryEngine, request_context):
    """combined (default) → each memory yields exactly one observation tagged
    with the memory's full tag set. With three disjoint tag sets, dispatch
    runs all three groups concurrently and each writes its own scope."""
    bank_id = f"test-combined-{uuid.uuid4().hex[:8]}"
    await memory.ensure_bank_profile(bank_id=bank_id, request_context=request_context)
    try:
        async with memory._pool.acquire() as conn:
            await _insert_memory(conn, bank_id, "Alice likes tea", ["user:alice"], None)
            await _insert_memory(conn, bank_id, "Bob bikes daily", ["user:bob"], None)
            await _insert_memory(conn, bank_id, "Carol reads books", ["user:carol"], None)

        wrapper, _ = _mock_llm_one_obs_per_fact()
        original_llm = memory._consolidation_llm_config
        memory._consolidation_llm_config = wrapper
        try:
            with (
                _override_config(memory, consolidation_llm_parallelism=3, consolidation_llm_batch_size=1),
                patch.object(memory, "submit_async_consolidation"),
            ):
                result = await run_consolidation_job(
                    memory_engine=memory, bank_id=bank_id, request_context=request_context
                )
        finally:
            memory._consolidation_llm_config = original_llm

        assert result["status"] == "completed"
        tag_sets = _ag_sorted(await _fetch_observation_tag_sets(memory, bank_id, request_context))
        assert tag_sets == _ag_sorted(
            [
                frozenset({"user:alice"}),
                frozenset({"user:bob"}),
                frozenset({"user:carol"}),
            ]
        )
    finally:
        await memory.delete_bank(bank_id, request_context=request_context)


@pytest.mark.asyncio
@pytest.mark.memory_backend_incompatible
async def test_shared_mode_parallel_writes_only_untagged_scope(memory: MemoryEngine, request_context):
    """shared → every memory writes to the single untagged scope, ignoring its
    own tags. Three memories with disjoint tags therefore all consolidate into
    the same global scope (the per-session-tag dedup use case) instead of one
    isolated observation per tag."""
    bank_id = f"test-shared-{uuid.uuid4().hex[:8]}"
    await memory.ensure_bank_profile(bank_id=bank_id, request_context=request_context)
    try:
        async with memory._pool.acquire() as conn:
            await _insert_memory(conn, bank_id, "Alice likes tea", ["session:s1"], "shared")
            await _insert_memory(conn, bank_id, "Bob bikes daily", ["session:s2"], "shared")
            await _insert_memory(conn, bank_id, "Carol reads books", ["session:s3"], "shared")

        wrapper, _ = _mock_llm_one_obs_per_fact()
        original_llm = memory._consolidation_llm_config
        memory._consolidation_llm_config = wrapper
        try:
            with (
                _override_config(memory, consolidation_llm_parallelism=3, consolidation_llm_batch_size=1),
                patch.object(memory, "submit_async_consolidation"),
            ):
                result = await run_consolidation_job(
                    memory_engine=memory, bank_id=bank_id, request_context=request_context
                )
        finally:
            memory._consolidation_llm_config = original_llm

        assert result["status"] == "completed"
        tag_sets = await _fetch_observation_tag_sets(memory, bank_id, request_context)
        # Every observation lands at the untagged scope — none carries a session tag.
        assert tag_sets and all(t == frozenset() for t in tag_sets), tag_sets
    finally:
        await memory.delete_bank(bank_id, request_context=request_context)


@pytest.mark.asyncio
@pytest.mark.memory_backend_incompatible
async def test_shared_mode_pools_different_native_tags_into_one_llm_batch(memory: MemoryEngine, request_context):
    """Regression test for #3953: two memories with different native tags,
    both requesting ``observation_scopes="shared"``, must be pooled into the
    same LLM consolidation batch/call — not split into two calls before their
    scope override is ever consulted.

    Before the fix, ``tag_groups`` keyed on each memory's raw native tag set,
    so these two memories (different tags) landed in two separate tag groups
    and were never presented to the LLM together, even though both named the
    identical target scope. ``consolidation_llm_batch_size=1`` (used by the
    sibling ``shared`` test above) doesn't exercise this: with batch size 1,
    every memory gets its own LLM call regardless of grouping. This test uses
    batch_size=2 so a single shared batch is actually observable.
    """
    bank_id = f"test-shared-pool-{uuid.uuid4().hex[:8]}"
    await memory.ensure_bank_profile(bank_id=bank_id, request_context=request_context)
    try:
        async with memory._pool.acquire() as conn:
            mem_a = await _insert_memory(
                conn, bank_id, "Terraform Cloud is our IaC tool", ["suggested_tool:terraform-cloud"], "shared"
            )
            mem_b = await _insert_memory(
                conn, bank_id, "Gardea helps with garden planning", ["suggested_tool:gardea"], "shared"
            )

        calls: list[set[str]] = []

        mock_llm = MockLLM(provider="mock", api_key="", base_url="", model="mock-model")

        def callback(messages, scope):
            if scope != "consolidation":
                return _ConsolidationBatchResponse()
            prompt = "\n".join(m.get("content", "") for m in messages if m.get("role") == "user")
            fact_ids = set(re.findall(r"\[([0-9a-f-]{36})\]", prompt))
            calls.append(fact_ids)
            creates = [
                _CreateAction(text=f"Observation about fact {fid[:8]}", source_fact_ids=[fid]) for fid in fact_ids
            ]
            return _ConsolidationBatchResponse(creates=creates)

        mock_llm.set_response_callback(callback)
        wrapper = MagicMock()
        wrapper.with_config.return_value = mock_llm

        original_llm = memory._consolidation_llm_config
        memory._consolidation_llm_config = wrapper
        try:
            with (
                _override_config(memory, consolidation_llm_parallelism=2, consolidation_llm_batch_size=2),
                patch.object(memory, "submit_async_consolidation"),
            ):
                result = await run_consolidation_job(
                    memory_engine=memory, bank_id=bank_id, request_context=request_context
                )
        finally:
            memory._consolidation_llm_config = original_llm

        assert result["status"] == "completed"
        assert len(calls) == 1, f"expected exactly one LLM consolidation call, got {len(calls)}: {calls}"
        assert calls[0] == {str(mem_a), str(mem_b)}, calls[0]
    finally:
        await memory.delete_bank(bank_id, request_context=request_context)


@pytest.mark.asyncio
@pytest.mark.memory_backend_incompatible
async def test_combined_and_per_tag_same_tags_do_not_share_a_batch(memory: MemoryEngine, request_context):
    """Regression test: a ``combined`` memory and a ``per_tag`` memory that
    happen to share the same native tags must never land in the same
    ``tag_groups`` bucket.

    Before this fix, ``_consolidation_batch_key`` fell back to native tags
    whenever a memory's resolved scope list had length != 1 (the per_tag/
    all_combinations/multi-scope-explicit fan-out branch), so a combined
    memory and a same-tagged per_tag memory both sorted to the same native
    tag tuple and collided into one group. Whichever memory then happened to
    land as ``sub_batch[0]`` after the ``llm_batch_size`` split decided the
    scope for *both* — silently dropping the per_tag fan-out, or wrongly
    fanning the combined memory out per tag.
    """
    bank_id = f"test-combined-per-tag-collision-{uuid.uuid4().hex[:8]}"
    await memory.ensure_bank_profile(bank_id=bank_id, request_context=request_context)
    try:
        async with memory._pool.acquire() as conn:
            await _insert_memory(conn, bank_id, "Alice likes tea and coffee", ["a", "b"], None)
            await _insert_memory(conn, bank_id, "Bob likes tea and coffee too", ["a", "b"], "per_tag")

        wrapper, _ = _mock_llm_one_obs_per_fact()
        original_llm = memory._consolidation_llm_config
        memory._consolidation_llm_config = wrapper
        try:
            with (
                _override_config(memory, consolidation_llm_parallelism=2, consolidation_llm_batch_size=2),
                patch.object(memory, "submit_async_consolidation"),
            ):
                result = await run_consolidation_job(
                    memory_engine=memory, bank_id=bank_id, request_context=request_context
                )
        finally:
            memory._consolidation_llm_config = original_llm

        assert result["status"] == "completed"
        tag_sets = _ag_sorted(await _fetch_observation_tag_sets(memory, bank_id, request_context))
        # combined memory -> one observation over its full tag set; per_tag
        # memory -> one observation per tag. Neither scope leaks into the other.
        assert tag_sets == _ag_sorted(
            [
                frozenset({"a", "b"}),
                frozenset({"a"}),
                frozenset({"b"}),
            ]
        )
    finally:
        await memory.delete_bank(bank_id, request_context=request_context)


@pytest.mark.asyncio
@pytest.mark.memory_backend_incompatible
async def test_per_tag_mode_parallel_writes_one_observation_per_tag(memory: MemoryEngine, request_context):
    """per_tag with tags [a, b] → two observations, tagged [a] and [b] respectively.

    Two memories with overlapping single-tag scopes ensure the parallel
    dispatcher must serialise on the shared scope and yields the same
    observation set as the sequential path."""
    bank_id = f"test-pertag-{uuid.uuid4().hex[:8]}"
    await memory.ensure_bank_profile(bank_id=bank_id, request_context=request_context)
    try:
        async with memory._pool.acquire() as conn:
            # M1: per_tag on [alice] -> writes obs at [alice]
            await _insert_memory(conn, bank_id, "Alice likes tea", ["alice"], "per_tag")
            # M2: per_tag on [alice, session] -> writes obs at [alice] AND [session]
            await _insert_memory(conn, bank_id, "Alice session detail", ["alice", "session"], "per_tag")

        wrapper, _ = _mock_llm_one_obs_per_fact()
        original_llm = memory._consolidation_llm_config
        memory._consolidation_llm_config = wrapper
        try:
            with (
                _override_config(memory, consolidation_llm_parallelism=4, consolidation_llm_batch_size=1),
                patch.object(memory, "submit_async_consolidation"),
            ):
                result = await run_consolidation_job(
                    memory_engine=memory, bank_id=bank_id, request_context=request_context
                )
        finally:
            memory._consolidation_llm_config = original_llm

        assert result["status"] == "completed"

        tag_sets = await _fetch_observation_tag_sets(memory, bank_id, request_context)
        # M1 writes to [alice]; M2 writes to [alice] and [session]. The mock LLM
        # creates one observation per fact per pass, so we expect:
        #   - one [alice] obs from M1
        #   - one [alice] obs from M2 (per_tag pass on alice)
        #   - one [session] obs from M2 (per_tag pass on session)
        alice_count = sum(1 for t in tag_sets if t == frozenset({"alice"}))
        session_count = sum(1 for t in tag_sets if t == frozenset({"session"}))
        assert alice_count == 2, f"expected 2 [alice] observations, got tag_sets={tag_sets}"
        assert session_count == 1, f"expected 1 [session] observation, got tag_sets={tag_sets}"
        # No observation should leak a tag set other than the per_tag scopes.
        assert all(t in (frozenset({"alice"}), frozenset({"session"})) for t in tag_sets), tag_sets
    finally:
        await memory.delete_bank(bank_id, request_context=request_context)


@pytest.mark.asyncio
@pytest.mark.memory_backend_incompatible
async def test_all_combinations_mode_parallel_writes_every_subset(memory: MemoryEngine, request_context):
    """all_combinations with tags [a, b] → three observations at [a], [b], [a, b]."""
    bank_id = f"test-allcombo-{uuid.uuid4().hex[:8]}"
    await memory.ensure_bank_profile(bank_id=bank_id, request_context=request_context)
    try:
        async with memory._pool.acquire() as conn:
            await _insert_memory(conn, bank_id, "Alice session detail", ["alice", "session"], "all_combinations")

        wrapper, _ = _mock_llm_one_obs_per_fact()
        original_llm = memory._consolidation_llm_config
        memory._consolidation_llm_config = wrapper
        try:
            with (
                _override_config(memory, consolidation_llm_parallelism=4, consolidation_llm_batch_size=1),
                patch.object(memory, "submit_async_consolidation"),
            ):
                result = await run_consolidation_job(
                    memory_engine=memory, bank_id=bank_id, request_context=request_context
                )
        finally:
            memory._consolidation_llm_config = original_llm

        assert result["status"] == "completed"
        tag_sets = set(await _fetch_observation_tag_sets(memory, bank_id, request_context))
        assert tag_sets == {
            frozenset({"alice"}),
            frozenset({"session"}),
            frozenset({"alice", "session"}),
        }
    finally:
        await memory.delete_bank(bank_id, request_context=request_context)


@pytest.mark.asyncio
@pytest.mark.memory_backend_incompatible
async def test_explicit_scope_list_parallel_writes_declared_scopes(memory: MemoryEngine, request_context):
    """Explicit list[list[str]] → observations land at exactly those scopes,
    regardless of the memory's own tag set."""
    bank_id = f"test-explicit-{uuid.uuid4().hex[:8]}"
    await memory.ensure_bank_profile(bank_id=bank_id, request_context=request_context)
    try:
        async with memory._pool.acquire() as conn:
            await _insert_memory(
                conn,
                bank_id,
                "Memory with explicit scopes",
                ["tag_ignored"],
                [["scope_a"], ["scope_b", "scope_c"]],
            )

        wrapper, _ = _mock_llm_one_obs_per_fact()
        original_llm = memory._consolidation_llm_config
        memory._consolidation_llm_config = wrapper
        try:
            with (
                _override_config(memory, consolidation_llm_parallelism=4, consolidation_llm_batch_size=1),
                patch.object(memory, "submit_async_consolidation"),
            ):
                result = await run_consolidation_job(
                    memory_engine=memory, bank_id=bank_id, request_context=request_context
                )
        finally:
            memory._consolidation_llm_config = original_llm

        assert result["status"] == "completed"
        tag_sets = set(await _fetch_observation_tag_sets(memory, bank_id, request_context))
        assert tag_sets == {frozenset({"scope_a"}), frozenset({"scope_b", "scope_c"})}
        # And NOT the memory's own tag.
        assert frozenset({"tag_ignored"}) not in tag_sets
    finally:
        await memory.delete_bank(bank_id, request_context=request_context)


@pytest.mark.asyncio
@pytest.mark.memory_backend_incompatible
async def test_empty_explicit_scope_list_does_not_pool_across_tags(memory: MemoryEngine, request_context):
    """An explicit ``observation_scopes: []`` resolves to no passes, so the pass
    loop falls back to the combined single pass over the memory's own tags. It
    must therefore batch as ``combined`` — keying it as a multi-scope fan-out
    gave every such memory the same tag-free key, pooling unrelated tag sets
    into one call whose ``obs_tags_override`` was then ``None``: both
    observations took ``memories[0]``'s tags and alice's observation carried
    bob's fact (or vice versa)."""
    bank_id = f"test-empty-scope-list-{uuid.uuid4().hex[:8]}"
    await memory.ensure_bank_profile(bank_id=bank_id, request_context=request_context)
    try:
        async with memory._pool.acquire() as conn:
            await _insert_memory(conn, bank_id, "Alice likes tea", ["user:alice"], [])
            await _insert_memory(conn, bank_id, "Bob bikes daily", ["user:bob"], [])

        wrapper, _ = _mock_llm_one_obs_per_fact()
        original_llm = memory._consolidation_llm_config
        memory._consolidation_llm_config = wrapper
        try:
            with (
                _override_config(memory, consolidation_llm_parallelism=2, consolidation_llm_batch_size=2),
                patch.object(memory, "submit_async_consolidation"),
            ):
                result = await run_consolidation_job(
                    memory_engine=memory, bank_id=bank_id, request_context=request_context
                )
        finally:
            memory._consolidation_llm_config = original_llm

        assert result["status"] == "completed"
        tag_sets = _ag_sorted(await _fetch_observation_tag_sets(memory, bank_id, request_context))
        assert tag_sets == _ag_sorted([frozenset({"user:alice"}), frozenset({"user:bob"})])
    finally:
        await memory.delete_bank(bank_id, request_context=request_context)


@pytest.mark.asyncio
@pytest.mark.memory_backend_incompatible
async def test_heterogeneous_batch_is_split_not_leaked(memory: MemoryEngine, request_context, caplog):
    """Defence in depth: even with the grouping key deliberately broken, a batch
    is never written at another memory's scope.

    ``_consolidation_batch_key`` is patched to a constant — the shape of every
    bug in this class, including the original #3953 native-tag key — so all four
    memories land in one group and one ``llm_batch_size=4`` batch. The sub-batch
    loop must notice the mixed scopes, split on the scopes the pass loop will
    actually write, and log the grouping bug, leaving every observation at its
    own scope: no untagged observation built from a tagged fact, no dropped
    ``shared`` override."""
    bank_id = f"test-hetero-split-{uuid.uuid4().hex[:8]}"
    await memory.ensure_bank_profile(bank_id=bank_id, request_context=request_context)
    try:
        async with memory._pool.acquire() as conn:
            await _insert_memory(conn, bank_id, "A session note", ["user:alice"], "shared")
            await _insert_memory(conn, bank_id, "Alice's salary is private", ["user:alice"], None)
            await _insert_memory(conn, bank_id, "Bob bikes daily", ["user:bob"], None)
            await _insert_memory(conn, bank_id, "Carol reads books", ["user:carol", "team:x"], "per_tag")

        wrapper, _ = _mock_llm_one_obs_per_fact()
        original_llm = memory._consolidation_llm_config
        memory._consolidation_llm_config = wrapper
        try:
            with (
                _override_config(memory, consolidation_llm_parallelism=1, consolidation_llm_batch_size=4),
                patch.object(memory, "submit_async_consolidation"),
                patch(
                    "hindsight_api.engine.consolidation.consolidator._consolidation_batch_key",
                    lambda _memory: ("everything-collides",),
                ),
                caplog.at_level(logging.ERROR, logger="hindsight_api.engine.consolidation.consolidator"),
            ):
                result = await run_consolidation_job(
                    memory_engine=memory, bank_id=bank_id, request_context=request_context
                )
        finally:
            memory._consolidation_llm_config = original_llm

        assert result["status"] == "completed"
        assert any("scope-homogeneous" in r.message for r in caplog.records), (
            "the broken grouping should have been reported"
        )
        tag_sets = _ag_sorted(await _fetch_observation_tag_sets(memory, bank_id, request_context))
        assert tag_sets == _ag_sorted(
            [
                frozenset(),  # the shared memory, at the untagged scope
                frozenset({"user:alice"}),
                frozenset({"user:bob"}),
                frozenset({"user:carol"}),  # per_tag fan-out, one per tag
                frozenset({"team:x"}),
            ]
        )
    finally:
        await memory.delete_bank(bank_id, request_context=request_context)


# ---------------------------------------------------------------------------
# Lock-serialisation test: groups whose write scopes share a scope must not
# have concurrent in-flight recalls / writes on the shared scope.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.memory_backend_incompatible
async def test_overlapping_scopes_serialise_under_parallelism(memory: MemoryEngine, request_context):
    """Two groups whose write-scope sets intersect on scope S must not have
    overlapping in-flight LLM-recall windows for S.

    Setup: M1 tagged [a] (per_tag → writes [a]) and M2 tagged [a, b] (per_tag
    → writes [a] and [b]). M1's lock set = {[a]}; M2's lock set = {[a], [b]}.
    They share scope [a], so under parallelism>1 they must serialise on [a].
    We wrap ``_find_related_observations`` to record per-scope entry/exit and
    assert max concurrency per scope == 1.
    """
    bank_id = f"test-locks-{uuid.uuid4().hex[:8]}"
    await memory.ensure_bank_profile(bank_id=bank_id, request_context=request_context)

    from hindsight_api.engine.consolidation import consolidator as consolidator_mod

    in_flight: dict[frozenset[str], int] = defaultdict(int)
    max_concurrent: dict[frozenset[str], int] = defaultdict(int)
    tracker_lock = asyncio.Lock()
    orig_find = consolidator_mod._find_related_observations

    async def tracked_find(*, memory_engine, bank_id, query, request_context, tags=None, config=None):
        scope = frozenset(tags or [])
        async with tracker_lock:
            in_flight[scope] += 1
            if in_flight[scope] > max_concurrent[scope]:
                max_concurrent[scope] = in_flight[scope]
        try:
            # Sleep to widen the window for races, so a missing lock would be visible.
            await asyncio.sleep(0.05)
            return await orig_find(
                memory_engine=memory_engine,
                bank_id=bank_id,
                query=query,
                request_context=request_context,
                tags=tags,
                config=config,
            )
        finally:
            async with tracker_lock:
                in_flight[scope] -= 1

    try:
        async with memory._pool.acquire() as conn:
            await _insert_memory(conn, bank_id, "Memory one alice only", ["a"], "per_tag")
            await _insert_memory(conn, bank_id, "Memory two alice and beta", ["a", "b"], "per_tag")

        wrapper, _ = _mock_llm_one_obs_per_fact()
        original_llm = memory._consolidation_llm_config
        memory._consolidation_llm_config = wrapper
        try:
            with (
                _override_config(memory, consolidation_llm_parallelism=4, consolidation_llm_batch_size=1),
                patch.object(memory, "submit_async_consolidation"),
                patch.object(consolidator_mod, "_find_related_observations", tracked_find),
            ):
                result = await run_consolidation_job(
                    memory_engine=memory, bank_id=bank_id, request_context=request_context
                )
        finally:
            memory._consolidation_llm_config = original_llm

        assert result["status"] == "completed"
        assert max_concurrent, "tracker saw no recalls — test did not exercise the dispatch path"

        # The whole point: lock invariant per scope.
        for scope, peak in max_concurrent.items():
            assert peak <= 1, (
                f"scope {set(scope) or '<untagged>'} had {peak} concurrent in-flight recalls; lock invariant violated"
            )
        # Sanity: we DID see recalls for the shared scope, so the test wasn't trivial.
        assert frozenset({"a"}) in max_concurrent
    finally:
        await memory.delete_bank(bank_id, request_context=request_context)


@pytest.mark.asyncio
@pytest.mark.memory_backend_incompatible
async def test_observation_md5_index_matches_python_unicode_whitespace(memory: MemoryEngine):
    """SQL index hash and Python normalization agree on all 29 whitespace codepoints."""
    from hindsight_api.engine.consolidation.consolidator import _NORMALIZED_OBS_SQL, _norm_obs_text

    whitespace = [chr(i) for i in range(0x110000) if chr(i).isspace()]
    assert len(whitespace) == 29
    expr = _NORMALIZED_OBS_SQL.replace("text,", "$1::text,")
    assert memory._pool is not None
    async with memory._pool.acquire() as conn:
        assert await conn.fetchval("SHOW server_encoding") == "UTF8"
        for char in whitespace:
            value = f"\tÅ{char}{char}BASiL{char}"
            expected = hashlib.md5(_norm_obs_text(value).encode("utf-8"), usedforsecurity=False).hexdigest()
            assert await conn.fetchval(f"SELECT md5({expr})", value) == expected, hex(ord(char))


@pytest.mark.asyncio
@pytest.mark.memory_backend_incompatible
@pytest.mark.parametrize("lane_parallelism,long_text", [(1, False), (2, False), (2, True)])
async def test_lane_exact_duplicate_create_without_semantic_dedup(
    memory: MemoryEngine, request_context, lane_parallelism: int, long_text: bool
):
    """A predecessor's verbatim CREATE cannot be repeated by a prepared sibling."""
    from hindsight_api.engine.consolidation import consolidator as mod

    bank_id = f"test-lane-exact-{uuid.uuid4().hex[:8]}"
    # Poorly compressible >10KB text used to exceed PostgreSQL's btree tuple limit.
    tail = " ".join(uuid.uuid4().hex for _ in range(400)) if long_text else ""
    first_text = "Identical   observation" + ("  " + tail if tail else "")
    second_text = "Identical observation" + (" " + tail if tail else "")
    assert not long_text or len(first_text.encode("utf-8")) > 10_000
    await memory.ensure_bank_profile(bank_id=bank_id, request_context=request_context)
    original_find = mod._find_related_observations
    original_exact_probe = mod._fetch_exact_observation_candidates
    fetched_candidate_counts: list[int] = []
    recalls = 0
    both_recalled = asyncio.Event()

    async def tracked_exact_probe(*args, **kwargs):
        rows = await original_exact_probe(*args, **kwargs)
        fetched_candidate_counts.append(len(rows))
        return rows

    async def synchronized_find(**kwargs):
        nonlocal recalls
        result = await original_find(**kwargs)
        recalls += 1
        if lane_parallelism == 2 and recalls <= 2:
            if recalls == 2:
                both_recalled.set()
            await asyncio.wait_for(both_recalled.wait(), timeout=10)
        return result

    mock_llm = MockLLM(provider="mock", api_key="", base_url="", model="mock-model")

    def response(messages, scope):
        if scope != "consolidation":
            return _ConsolidationBatchResponse()
        prompt = "\n".join(m.get("content", "") for m in messages if m.get("role") == "user")
        fact_id = next(str(fid) for fid in fact_ids if str(fid) in prompt)
        return _ConsolidationBatchResponse(
            creates=[
                _CreateAction(
                    text=first_text if fact_id == str(fact_ids[0]) else second_text,
                    source_fact_ids=[fact_id],
                )
            ]
        )

    mock_llm.set_response_callback(response)
    wrapper = MagicMock()
    wrapper.with_config.return_value = mock_llm
    fact_ids = []
    try:
        async with memory._pool.acquire() as conn:
            if long_text:
                assert await conn.fetchval("SHOW server_encoding") == "UTF8"
                index_def = await conn.fetchval(
                    "SELECT indexdef FROM pg_indexes WHERE schemaname = current_schema() "
                    "AND indexname = 'idx_memory_units_observation_norm_text_md5'"
                )
                assert index_def and "md5(" in index_def
            for index in range(2):
                fact_ids.append(await _insert_memory(conn, bank_id, f"Source fact {index}", ["same"], "shared"))
            await conn.executemany(
                """
                INSERT INTO memory_units
                    (id, bank_id, text, fact_type, tags, source_memory_ids, consolidated_at, created_at)
                VALUES ($1, $2, $3, 'observation', $4, '{}', now(), now())
                """,
                [(uuid.uuid4(), bank_id, f"Unrelated observation {index}", ["same"]) for index in range(2000)],
            )
        with (
            patch.object(memory, "_consolidation_llm_config", wrapper),
            patch.object(memory, "submit_async_consolidation"),
            patch.object(mod, "_find_related_observations", synchronized_find),
            patch.object(mod, "_fetch_exact_observation_candidates", tracked_exact_probe),
            _override_config(
                memory,
                consolidation_llm_parallelism=2,
                consolidation_lane_llm_parallelism=lane_parallelism,
                consolidation_llm_batch_size=1,
                consolidation_batch_size=2,
                consolidation_dedup_threshold=1.0,
            ),
        ):
            result = await asyncio.wait_for(
                run_consolidation_job(memory_engine=memory, bank_id=bank_id, request_context=request_context),
                timeout=20,
            )
        async with memory._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT text FROM memory_units WHERE bank_id=$1 AND fact_type='observation'", bank_id
            )
        assert result["status"] == "completed"
        assert recalls >= 2
        assert len(rows) == 2001
        assert sum(row["text"] == first_text for row in rows) == 1
        if lane_parallelism == 1:
            assert fetched_candidate_counts == []
        else:
            assert fetched_candidate_counts and max(fetched_candidate_counts) <= 1
    finally:
        await memory.delete_bank(bank_id, request_context=request_context)


@pytest.mark.asyncio
@pytest.mark.memory_backend_incompatible
async def test_same_lane_parallelizes_llm_but_serializes_apply(memory: MemoryEngine, request_context):
    """Batches in one shared lane overlap in LLM time but never overlap DB apply."""
    bank_id = f"test-lane-parallel-{uuid.uuid4().hex[:8]}"
    await memory.ensure_bank_profile(bank_id=bank_id, request_context=request_context)
    from hindsight_api.engine.consolidation import consolidator as consolidator_mod

    llm_in_flight = 0
    max_llm_in_flight = 0
    apply_in_flight = 0
    max_apply_in_flight = 0
    tracker_lock = asyncio.Lock()
    all_llm_entered = asyncio.Event()
    all_apply_ready = asyncio.Event()
    apply_ready: set[str] = set()
    apply_order: list[str] = []
    original_llm = consolidator_mod._consolidate_batch_with_llm
    original_apply = consolidator_mod._apply_create_action
    original_process = consolidator_mod._process_memory_batch

    async def synchronized_llm(*args, **kwargs):
        nonlocal llm_in_flight, max_llm_in_flight
        async with tracker_lock:
            llm_in_flight += 1
            max_llm_in_flight = max(max_llm_in_flight, llm_in_flight)
            if llm_in_flight == 4:
                all_llm_entered.set()
        try:
            # Hold actual LLM calls until all four preparations reach this
            # boundary. Database/embedding latency cannot shorten the overlap.
            await asyncio.wait_for(all_llm_entered.wait(), timeout=10)
            return await original_llm(*args, **kwargs)
        finally:
            async with tracker_lock:
                llm_in_flight -= 1

    async def tracked_process(*args, **kwargs):
        if kwargs["apply_turn"] is None:
            # A regression to the serial path must fail the LLM rendezvous,
            # rather than failing solely because instrumentation requires a lane.
            return await original_process(*args, **kwargs)
        turn, successor = kwargs["apply_turn"]
        text = kwargs["memories"][0]["text"]

        class TrackedTurn:
            async def wait(self):
                apply_ready.add(text)
                if len(apply_ready) == 4:
                    all_apply_ready.set()
                await turn.wait()

        kwargs["apply_turn"] = (TrackedTurn(), successor)
        return await original_process(*args, **kwargs)

    async def tracked_apply(*args, **kwargs):
        nonlocal apply_in_flight, max_apply_in_flight
        async with tracker_lock:
            apply_in_flight += 1
            max_apply_in_flight = max(max_apply_in_flight, apply_in_flight)
        try:
            # Keep the first transaction open until every contender reaches its
            # apply turn. A missing serialization fence now overlaps or reorders
            # these real writes instead of depending on a short sleep.
            await asyncio.wait_for(all_apply_ready.wait(), timeout=10)
            apply_order.append(kwargs["prepared"].source_mems[0]["text"])
            return await original_apply(*args, **kwargs)
        finally:
            async with tracker_lock:
                apply_in_flight -= 1

    try:
        async with memory._pool.acquire() as conn:
            for index in range(4):
                await _insert_memory(conn, bank_id, f"Shared fact {index}", [f"native:{index}"], "shared")
        wrapper, _ = _mock_llm_one_obs_per_fact()
        original_config_llm = memory._consolidation_llm_config
        memory._consolidation_llm_config = wrapper
        try:
            with (
                _override_config(
                    memory,
                    consolidation_llm_parallelism=4,
                    consolidation_lane_llm_parallelism=4,
                    consolidation_llm_batch_size=1,
                    consolidation_dedup_threshold=1.0,
                ),
                patch.object(memory, "submit_async_consolidation"),
                patch.object(consolidator_mod, "_consolidate_batch_with_llm", synchronized_llm),
                patch.object(consolidator_mod, "_process_memory_batch", tracked_process),
                patch.object(consolidator_mod, "_apply_create_action", tracked_apply),
            ):
                result = await asyncio.wait_for(
                    run_consolidation_job(memory_engine=memory, bank_id=bank_id, request_context=request_context),
                    timeout=30,
                )
        finally:
            memory._consolidation_llm_config = original_config_llm
        assert result["status"] == "completed"
        assert max_llm_in_flight == 4
        assert max_apply_in_flight == 1
        assert apply_order == [f"Shared fact {index}" for index in range(4)]
    finally:
        await memory.delete_bank(bank_id, request_context=request_context)


@pytest.mark.asyncio
@pytest.mark.memory_backend_incompatible
async def test_failed_preparation_does_not_release_later_apply_early(memory: MemoryEngine, request_context):
    """A failed second batch cannot let the third pass a slow first apply."""
    from hindsight_api.engine.consolidation import consolidator as mod

    bank_id = f"test-lane-failed-order-{uuid.uuid4().hex[:8]}"
    await memory.ensure_bank_profile(bank_id=bank_id, request_context=request_context)
    try:
        async with memory._pool.acquire() as conn:
            for index in range(3):
                await _insert_memory(conn, bank_id, f"Order fact {index}", ["shared"], None)
        wrapper, _ = _mock_llm_one_obs_per_fact()
        applied: list[str] = []
        first_applying = asyncio.Event()
        second_failed = asyncio.Event()
        original_process = mod._process_memory_batch
        original_apply = mod._apply_create_action

        async def fail_second(*args, **kwargs):
            text = kwargs["memories"][0]["text"]
            if text == "Order fact 1":
                await first_applying.wait()
                second_failed.set()
                raise mod._InvalidConsolidationReferences("deterministic invalid response")
            return await original_process(*args, **kwargs)

        async def slow_first(*args, **kwargs):
            text = kwargs["prepared"].source_mems[0]["text"]
            if text == "Order fact 0":
                first_applying.set()
                await second_failed.wait()
                await asyncio.sleep(0.15)
            applied.append(text)
            return await original_apply(*args, **kwargs)

        with (
            patch.object(memory, "_consolidation_llm_config", wrapper),
            patch.object(memory, "submit_async_consolidation"),
            patch.object(mod, "_process_memory_batch", fail_second),
            patch.object(mod, "_apply_create_action", slow_first),
            _override_config(
                memory,
                consolidation_llm_parallelism=3,
                consolidation_lane_llm_parallelism=3,
                consolidation_llm_batch_size=1,
                consolidation_dedup_threshold=1.0,
            ),
        ):
            result = await asyncio.wait_for(
                run_consolidation_job(memory_engine=memory, bank_id=bank_id, request_context=request_context), 15
            )
        assert result["status"] == "completed"
        assert applied == ["Order fact 0", "Order fact 2"]
    finally:
        await memory.delete_bank(bank_id, request_context=request_context)


@pytest.mark.asyncio
@pytest.mark.memory_backend_incompatible
async def test_cancel_lane_before_predecessor_turn_does_not_hang(memory: MemoryEngine, request_context):
    """Cancellation while the first batch prepares unwinds all lane waiters."""
    from hindsight_api.engine.consolidation import consolidator as mod

    bank_id = f"test-lane-cancel-turn-{uuid.uuid4().hex[:8]}"
    await memory.ensure_bank_profile(bank_id=bank_id, request_context=request_context)
    try:
        async with memory._pool.acquire() as conn:
            for index in range(3):
                await _insert_memory(conn, bank_id, f"Cancel fact {index}", ["shared"], None)
        wrapper, _ = _mock_llm_one_obs_per_fact()
        entered = asyncio.Event()
        original = mod._process_memory_batch

        async def held(*args, **kwargs):
            if kwargs["memories"][0]["text"] == "Cancel fact 0":
                entered.set()
                await asyncio.Event().wait()
            return await original(*args, **kwargs)

        with (
            patch.object(memory, "_consolidation_llm_config", wrapper),
            patch.object(memory, "submit_async_consolidation"),
            patch.object(mod, "_process_memory_batch", held),
            _override_config(
                memory,
                consolidation_llm_parallelism=3,
                consolidation_lane_llm_parallelism=3,
                consolidation_llm_batch_size=1,
                consolidation_dedup_threshold=1.0,
            ),
        ):
            job = asyncio.create_task(
                run_consolidation_job(memory_engine=memory, bank_id=bank_id, request_context=request_context)
            )
            await asyncio.wait_for(entered.wait(), 10)
            job.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(job, 10)
    finally:
        await memory.delete_bank(bank_id, request_context=request_context)


@pytest.mark.asyncio
@pytest.mark.memory_backend_incompatible
async def test_same_lane_stale_update_retries_fresh_recall(memory: MemoryEngine, request_context):
    """Both singleton updates commit; the second re-recalls after the first apply."""
    bank_id = f"test-lane-stale-{uuid.uuid4().hex[:8]}"
    await memory.ensure_bank_profile(bank_id=bank_id, request_context=request_context)
    from hindsight_api.engine.consolidation import consolidator as consolidator_mod

    fact_ids: list[uuid.UUID] = []
    observation_id = uuid.uuid4()
    try:
        async with memory._pool.acquire() as conn:
            for index in range(2):
                fact_ids.append(
                    await _insert_memory(conn, bank_id, f"Stale source {index}", [f"native:{index}"], "shared")
                )
            await conn.execute(
                """
                INSERT INTO memory_units (id, bank_id, text, fact_type, tags, source_memory_ids, created_at)
                VALUES ($1, $2, $3, 'observation', '{}', $4, now())
                """,
                observation_id,
                bank_id,
                "Original observation",
                [str(fact_ids[0])],
            )

        wrapper = MagicMock()
        mock_llm = MockLLM(provider="mock", api_key="", base_url="", model="mock-model")

        def update_callback(messages, scope):
            if scope != "consolidation":
                return _ConsolidationBatchResponse()
            prompt = "\n".join(m.get("content", "") for m in messages if m.get("role") == "user")
            source_id = next(str(fact_id) for fact_id in fact_ids if str(fact_id) in prompt)
            return _ConsolidationBatchResponse(
                updates=[
                    _UpdateAction(
                        observation_id=str(observation_id),
                        text=f"Updated from {source_id[:8]}",
                        source_fact_ids=[source_id],
                    )
                ]
            )

        mock_llm.set_response_callback(update_callback)
        wrapper.with_config.return_value = mock_llm

        recall_count = 0
        initial_recalls_ready = asyncio.Event()

        async def fake_find(*, memory_engine, bank_id, query, request_context, tags=None, config=None):
            nonlocal recall_count
            recall_count += 1
            if recall_count == 2:
                initial_recalls_ready.set()
            async with memory._pool.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT text, source_memory_ids FROM memory_units WHERE id = $1", observation_id
                )
            observed = RecallResult.model_construct(
                results=[
                    MemoryFact.model_construct(
                        id=str(observation_id),
                        text=row["text"],
                        fact_type="observation",
                        tags=[],
                        source_fact_ids=list(row["source_memory_ids"]),
                    )
                ]
            )
            if recall_count <= 2:
                await initial_recalls_ready.wait()
            return observed

        original_config_llm = memory._consolidation_llm_config
        memory._consolidation_llm_config = wrapper
        try:
            with (
                _override_config(
                    memory,
                    consolidation_llm_parallelism=2,
                    consolidation_lane_llm_parallelism=2,
                    consolidation_llm_batch_size=1,
                    consolidation_dedup_threshold=1.0,
                ),
                patch.object(memory, "submit_async_consolidation"),
                patch.object(consolidator_mod, "_find_related_observations", fake_find),
            ):
                result = await run_consolidation_job(
                    memory_engine=memory, bank_id=bank_id, request_context=request_context
                )
        finally:
            memory._consolidation_llm_config = original_config_llm

        assert result["status"] == "completed"
        async with memory._pool.acquire() as conn:
            row = await conn.fetchrow("SELECT text FROM memory_units WHERE id = $1", observation_id)
            states = await conn.fetch(
                "SELECT consolidated_at, consolidation_failed_at FROM memory_units WHERE id = ANY($1::uuid[])",
                fact_ids,
            )
        assert row["text"] == f"Updated from {str(fact_ids[1])[:8]}"
        assert recall_count >= 3
        assert all(state["consolidation_failed_at"] is None for state in states)
        assert sum(state["consolidated_at"] is not None for state in states) == 2
    finally:
        await memory.delete_bank(bank_id, request_context=request_context)


@pytest.mark.asyncio
@pytest.mark.memory_backend_incompatible
@pytest.mark.parametrize("invalidate", [False, True], ids=["delete", "invalidate"])
async def test_lane_apply_waits_for_source_before_observation_during_delete_sweep(
    memory: MemoryEngine, request_context, invalidate: bool
):
    """PG deletion/invalidation can sweep while lane apply waits on its source.

    With the old observation-first validator, the sweep's observation lock waited
    on apply while apply waited on the outgoing source: a lock-order inversion.
    """
    from hindsight_api.engine.consolidation import consolidator as mod

    bank_id = f"test-lane-sweep-{uuid.uuid4().hex[:8]}"
    await memory.ensure_bank_profile(bank_id=bank_id, request_context=request_context)
    # Force the sweep's UUID ordering to acquire the outgoing fact before the
    # observation, so the old observation-first apply reliably exposes the cycle.
    fact_id = uuid.UUID(int=uuid.uuid4().int & ((1 << 120) - 1))
    obs_id = uuid.uuid4()
    job = None
    try:
        async with memory._pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO memory_units (id, bank_id, text, fact_type, tags, created_at) "
                "VALUES ($1,$2,'Outgoing source','experience',$3,now())",
                fact_id,
                bank_id,
                ["shared"],
            )
            await conn.execute(
                "INSERT INTO memory_units (id, bank_id, text, fact_type, tags, source_memory_ids, created_at) "
                "VALUES ($1,$2,'Original observation','observation',$3,$4,now())",
                obs_id,
                bank_id,
                ["shared"],
                [str(fact_id)],
            )

        async def recalled(**kwargs):
            async with memory._pool.acquire() as conn:
                row = await conn.fetchrow("SELECT text, source_memory_ids FROM memory_units WHERE id=$1", obs_id)
            return RecallResult.model_construct(
                results=[
                    MemoryFact.model_construct(
                        id=str(obs_id),
                        text=row["text"],
                        fact_type="observation",
                        tags=["shared"],
                        source_fact_ids=list(row["source_memory_ids"]),
                    )
                ]
                if row
                else []
            )

        wrapper = MagicMock()
        llm = MockLLM(provider="mock", api_key="", base_url="", model="mock-model")
        llm.set_response_callback(
            lambda messages, scope: _ConsolidationBatchResponse(
                updates=[
                    _UpdateAction(
                        observation_id=str(obs_id),
                        text="Should not survive deletion",
                        source_fact_ids=[str(fact_id)],
                    )
                ]
            )
            if scope == "consolidation"
            else _ConsolidationBatchResponse()
        )
        wrapper.with_config.return_value = llm
        with (
            patch.object(memory, "_consolidation_llm_config", wrapper),
            patch.object(memory, "submit_async_consolidation"),
            patch.object(mod, "_find_related_observations", recalled),
            _override_config(
                memory,
                consolidation_llm_parallelism=2,
                consolidation_lane_llm_parallelism=2,
                consolidation_llm_batch_size=1,
                consolidation_dedup_threshold=1.0,
            ),
        ):
            async with memory._pool.acquire() as deleting_conn:
                async with deleting_conn.transaction():
                    holder_pid = await deleting_conn.fetchval("SELECT pg_backend_pid()")
                    await deleting_conn.execute("SET LOCAL lock_timeout = '1000ms'")
                    await deleting_conn.fetchrow("SELECT id FROM memory_units WHERE id=$1 FOR UPDATE", fact_id)
                    job = asyncio.create_task(
                        run_consolidation_job(memory_engine=memory, bank_id=bank_id, request_context=request_context)
                    )
                    # Wait for real PG lock contention, not a timing guess. The
                    # blocked backend belongs to apply, which has finished recall.
                    deadline = time.monotonic() + 10
                    while True:
                        async with memory._pool.acquire() as observer:
                            blocked = await observer.fetchval(
                                "SELECT EXISTS (SELECT 1 FROM pg_stat_activity "
                                "WHERE $1 = ANY(pg_blocking_pids(pid)) "
                                "AND query LIKE '%memory_units%')",
                                holder_pid,
                            )
                        if blocked:
                            break
                        assert not job.done(), "lane job ended before the source lock was contested"
                        assert time.monotonic() < deadline, "lane apply did not reach PG source lock"
                        await asyncio.sleep(0.02)
                    # The same transaction owns the source lock, so the sweep
                    # must reach the observation without waiting for lane apply.
                    await memory._delete_stale_observations_for_memories(deleting_conn, bank_id, [str(fact_id)])
                    if invalidate:
                        assert await mod.get_memories().invalidate_memory(
                            conn=deleting_conn,
                            fq_table=mod.fq_table,
                            bank_id=bank_id,
                            unit_id=str(fact_id),
                            reason="concurrent invalidation",
                        )
                    else:
                        await deleting_conn.execute("DELETE FROM memory_units WHERE id=$1", fact_id)
            result = await asyncio.wait_for(job, timeout=15)
        assert result["status"] == "completed"
        async with memory._pool.acquire() as conn:
            assert await conn.fetchval("SELECT id FROM memory_units WHERE id=$1", obs_id) is None
            assert await conn.fetchval("SELECT id FROM memory_units WHERE id=$1", fact_id) is None
            if invalidate:
                assert await conn.fetchval("SELECT id FROM invalidated_memory_units WHERE id=$1", fact_id) == fact_id
    finally:
        if job is not None and not job.done():
            job.cancel()
            await asyncio.gather(job, return_exceptions=True)
        await memory.delete_bank(bank_id, request_context=request_context)


@pytest.mark.asyncio
@pytest.mark.memory_backend_incompatible
async def test_lane_partial_invalid_reply_and_stale_sibling_retry(memory: MemoryEngine, request_context):
    """A partially accepted batch drains its uncovered fact before its stale sibling applies."""
    bank_id = f"test-lane-partial-stale-{uuid.uuid4().hex[:8]}"
    await memory.ensure_bank_profile(bank_id=bank_id, request_context=request_context)
    from hindsight_api.engine.consolidation import consolidator as consolidator_mod

    fact_ids: list[uuid.UUID] = []
    observation_id = uuid.uuid4()
    outsider = str(uuid.uuid4())
    from tests.test_consolidation_batch_atomicity import _llm

    try:
        async with memory._pool.acquire() as conn:
            for index in range(3):
                fact_ids.append(await _insert_memory(conn, bank_id, f"Lane partial fact {index}", ["shared"], "shared"))
            await conn.execute(
                """INSERT INTO memory_units (id, bank_id, text, fact_type, tags, source_memory_ids, created_at)
                   VALUES ($1, $2, 'Original observation', 'observation', '{}', $3, now())""",
                observation_id,
                bank_id,
                [str(fact_ids[0])],
            )

        calls: list[str] = []

        def response(messages, scope):
            assert scope == "consolidation"
            prompt = "\n".join(m.get("content", "") for m in messages if m.get("role") == "user")
            calls.append(prompt)
            if "Lane partial fact 0" in prompt and "Lane partial fact 1" in prompt:
                return _ConsolidationBatchResponse(
                    updates=[
                        _UpdateAction(
                            observation_id=str(observation_id),
                            text="Updated by first fact",
                            source_fact_ids=[str(fact_ids[0])],
                        )
                    ],
                    creates=[
                        _CreateAction(
                            text="Invalid citation",
                            source_fact_ids=[str(fact_ids[1]), outsider],
                        )
                    ],
                )
            if "Lane partial fact 1" in prompt:
                return _ConsolidationBatchResponse(
                    creates=[
                        _CreateAction(
                            text="Covered second fact",
                            source_fact_ids=[str(fact_ids[1])],
                        )
                    ]
                )
            assert "Lane partial fact 2" in prompt
            return _ConsolidationBatchResponse(
                updates=[
                    _UpdateAction(
                        observation_id=str(observation_id),
                        text="Updated by third fact",
                        source_fact_ids=[str(fact_ids[2])],
                    )
                ]
            )

        initial_recalls = 0
        both_recalled = asyncio.Event()

        async def fake_find(*, memory_engine, bank_id, query, request_context, tags=None, config=None):
            nonlocal initial_recalls
            initial_recalls += 1
            if initial_recalls == 2:
                both_recalled.set()
            async with memory._pool.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT text, source_memory_ids FROM memory_units WHERE id = $1", observation_id
                )
            if initial_recalls <= 2:
                await both_recalled.wait()
            return RecallResult.model_construct(
                results=[
                    MemoryFact.model_construct(
                        id=str(observation_id),
                        text=row["text"],
                        fact_type="observation",
                        tags=[],
                        source_fact_ids=list(row["source_memory_ids"]),
                    )
                ]
            )

        with (
            patch.object(memory, "_consolidation_llm_config", _llm(response)),
            patch.object(memory, "submit_async_consolidation"),
            patch.object(consolidator_mod, "_find_related_observations", fake_find),
            _override_config(
                memory,
                consolidation_llm_parallelism=2,
                consolidation_lane_llm_parallelism=2,
                consolidation_llm_batch_size=2,
                consolidation_batch_size=3,
                consolidation_dedup_threshold=1.0,
                llm_language_integrity="off",
            ),
        ):
            result = await asyncio.wait_for(
                run_consolidation_job(memory_engine=memory, bank_id=bank_id, request_context=request_context),
                timeout=15,
            )
        assert result["status"] == "completed"
        assert result["memories_failed"] == 0
        assert len(calls) == 4  # partial first reply, B-only retry, stale C reply, fresh C retry
        async with memory._pool.acquire() as conn:
            observation = await conn.fetchrow("SELECT text FROM memory_units WHERE id=$1", observation_id)
            covered = await conn.fetchrow(
                "SELECT source_memory_ids FROM memory_units WHERE bank_id=$1 AND text='Covered second fact'", bank_id
            )
            states = await conn.fetch(
                "SELECT id, consolidated_at, consolidation_failed_at FROM memory_units WHERE id = ANY($1::uuid[])",
                fact_ids,
            )
        assert observation["text"] == "Updated by third fact"
        assert covered["source_memory_ids"] == [fact_ids[1]]
        assert all(
            state["consolidated_at"] is not None and state["consolidation_failed_at"] is None for state in states
        )
    finally:
        await memory.delete_bank(bank_id, request_context=request_context)


@pytest.mark.asyncio
@pytest.mark.memory_backend_incompatible
async def test_lane_limit_is_independent_of_global_limit(memory: MemoryEngine, request_context):
    """Two lanes each stay at two LLM calls, yet together use more than two."""
    bank_id = f"test-lane-limit-{uuid.uuid4().hex[:8]}"
    await memory.ensure_bank_profile(bank_id=bank_id, request_context=request_context)
    from hindsight_api.engine.consolidation import consolidator as consolidator_mod

    in_flight = {"a": 0, "b": 0}
    peak = {"a": 0, "b": 0}
    global_peak = 0
    first_wave = asyncio.Event()
    original = consolidator_mod._consolidate_batch_with_llm

    async def tracked(*args, **kwargs):
        nonlocal global_peak
        lane = kwargs["memories"][0]["tags"][0]
        in_flight[lane] += 1
        peak[lane] = max(peak[lane], in_flight[lane])
        global_peak = max(global_peak, sum(in_flight.values()))
        try:
            # Synchronize the first calls instead of assuming database reads
            # finish within an 80ms overlap window on a busy CI worker.
            if all(value >= 2 for value in peak.values()):
                first_wave.set()
            await asyncio.wait_for(first_wave.wait(), timeout=30)
            return await original(*args, **kwargs)
        finally:
            in_flight[lane] -= 1

    try:
        async with memory._pool.acquire() as conn:
            for lane in ("a", "b"):
                for index in range(4):
                    await _insert_memory(conn, bank_id, f"{lane} fact {index}", [lane], None)
        wrapper, _ = _mock_llm_one_obs_per_fact()
        previous = memory._consolidation_llm_config
        memory._consolidation_llm_config = wrapper
        try:
            with (
                _override_config(
                    memory,
                    consolidation_llm_parallelism=4,
                    consolidation_lane_llm_parallelism=2,
                    consolidation_llm_batch_size=1,
                    consolidation_dedup_threshold=1.0,
                ),
                patch.object(memory, "submit_async_consolidation"),
                patch.object(consolidator_mod, "_consolidate_batch_with_llm", tracked),
            ):
                result = await run_consolidation_job(
                    memory_engine=memory, bank_id=bank_id, request_context=request_context
                )
        finally:
            memory._consolidation_llm_config = previous
        assert result["status"] == "completed"
        assert all(1 < value <= 2 for value in peak.values()), peak
        assert global_peak > 2
    finally:
        await memory.delete_bank(bank_id, request_context=request_context)


@pytest.mark.asyncio
@pytest.mark.memory_backend_incompatible
@pytest.mark.parametrize(
    "error,failed",
    [
        ("stale", False),
        ("invalid", True),
    ],
)
async def test_reference_failure_lifecycle(memory: MemoryEngine, request_context, error, failed):
    """Exhausted conflicts stay pending; genuine invalid content is marked failed."""
    bank_id = f"test-reference-failure-{uuid.uuid4().hex[:8]}"
    await memory.ensure_bank_profile(bank_id=bank_id, request_context=request_context)
    from hindsight_api.engine.consolidation import consolidator as consolidator_mod

    try:
        async with memory._pool.acquire() as conn:
            fact_id = await _insert_memory(conn, bank_id, "Reference failure fact", ["a"], "shared")
            healthy_id = await _insert_memory(conn, bank_id, "Healthy fact", ["b"], "shared")
        wrapper, _ = _mock_llm_one_obs_per_fact()
        previous = memory._consolidation_llm_config
        memory._consolidation_llm_config = wrapper
        original = consolidator_mod._process_memory_batch
        attempts = 0

        async def simulated(*args, **kwargs):
            nonlocal attempts
            if kwargs["memories"][0]["id"] == fact_id:
                attempts += 1
                exception = (
                    consolidator_mod._InvalidConsolidationReferences
                    if failed
                    else consolidator_mod._StaleConsolidationReference
                )
                raise exception("simulated reference mismatch")
            return await original(*args, **kwargs)

        try:
            with (
                _override_config(
                    memory,
                    consolidation_llm_parallelism=2,
                    consolidation_lane_llm_parallelism=2,
                    consolidation_llm_batch_size=1,
                    consolidation_batch_size=1,
                    consolidation_dedup_threshold=1.0,
                ),
                patch.object(memory, "submit_async_consolidation"),
                patch.object(consolidator_mod, "_process_memory_batch", simulated),
            ):
                result = await asyncio.wait_for(
                    run_consolidation_job(memory_engine=memory, bank_id=bank_id, request_context=request_context),
                    timeout=15,
                )
        finally:
            memory._consolidation_llm_config = previous
        assert result["status"] == "completed"
        assert attempts == (1 if failed else 3)
        async with memory._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT id, consolidated_at, consolidation_failed_at FROM memory_units WHERE id = ANY($1::uuid[])",
                [fact_id, healthy_id],
            )
        states = {row["id"]: row for row in rows}
        assert (states[fact_id]["consolidation_failed_at"] is not None) == failed
        assert states[fact_id]["consolidated_at"] is None
        assert states[healthy_id]["consolidated_at"] is not None
        assert states[healthy_id]["consolidation_failed_at"] is None
    finally:
        await memory.delete_bank(bank_id, request_context=request_context)


# ---------------------------------------------------------------------------
# Disjoint scopes actually run concurrently (the throughput justification for
# the whole feature). Without this, the "lock-on-everything" implementation
# would still pass the safety tests.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.memory_backend_incompatible
async def test_per_batch_log_line_attributes_only_own_work(memory: MemoryEngine, request_context, caplog):
    """Per-batch log timings / llm_calls / tokens / processed must reflect only
    that batch's own work — not totals leaking in from other in-flight batches
    under parallelism. Pins the per-batch perf isolation that ``batch_perf``
    + ``perf.merge_from`` provide.

    Setup: 3 disjoint memories under combined mode at parallelism=3 so all
    three batches run concurrently. The mock LLM is deterministic (1 obs per
    fact, 1 LLM call per batch). After the job we parse each emitted log line
    and assert per-batch attributes against that single batch's known work,
    plus check the cumulative ``processed=N/total`` field is monotonic.
    """
    import logging

    bank_id = f"test-perbatch-log-{uuid.uuid4().hex[:8]}"
    await memory.ensure_bank_profile(bank_id=bank_id, request_context=request_context)
    try:
        async with memory._pool.acquire() as conn:
            await _insert_memory(conn, bank_id, "Alice fact", ["alice"], None)
            await _insert_memory(conn, bank_id, "Bob fact", ["bob"], None)
            await _insert_memory(conn, bank_id, "Carol fact", ["carol"], None)

        wrapper, _ = _mock_llm_one_obs_per_fact()
        original_llm = memory._consolidation_llm_config
        memory._consolidation_llm_config = wrapper
        try:
            with (
                _override_config(memory, consolidation_llm_parallelism=3, consolidation_llm_batch_size=1),
                patch.object(memory, "submit_async_consolidation"),
                caplog.at_level(logging.INFO, logger="hindsight_api.engine.consolidation.consolidator"),
            ):
                await run_consolidation_job(memory_engine=memory, bank_id=bank_id, request_context=request_context)
        finally:
            memory._consolidation_llm_config = original_llm

        # Parse the per-batch log lines.
        per_batch = [r.message for r in caplog.records if "llm_batch #" in r.message]
        assert len(per_batch) == 3, f"expected 3 per-batch log lines, got {len(per_batch)}:\n{per_batch}"

        # Every batch processed exactly one memory (llm_batch_size=1) and made
        # exactly one LLM call. If a stale-snapshot bug let counters from
        # other batches leak in, ``llm calls`` would be > 1 for some batches.
        processed_values: list[int] = []
        for line in per_batch:
            m_calls = re.search(r"(\d+) llm calls", line)
            assert m_calls and int(m_calls.group(1)) == 1, f"expected 1 llm call per batch, got: {line}"
            m_mems = re.search(r"\((\d+) memories,", line)
            assert m_mems and int(m_mems.group(1)) == 1, f"expected 1 memory per batch, got: {line}"
            # Per-batch created count must be 1 (mock LLM creates one obs per fact).
            m_created = re.search(r"created=(\d+)", line)
            assert m_created and int(m_created.group(1)) == 1, f"expected created=1, got: {line}"
            # processed=N/3 — cumulative; collect for monotonicity check.
            m_proc = re.search(r"processed=(\d+)/3", line)
            assert m_proc, f"expected processed=N/3 cumulative indicator, got: {line}"
            processed_values.append(int(m_proc.group(1)))

        # Cumulative counter must be monotonically increasing and end at 3.
        assert processed_values == sorted(processed_values), (
            f"processed counter must be monotonic, got {processed_values}"
        )
        assert max(processed_values) == 3, f"final cumulative processed should be 3, got {max(processed_values)}"
        assert set(processed_values) == {1, 2, 3}, (
            f"each batch should bump the counter by exactly 1, got {processed_values}"
        )

        # Per-batch llm timing must be > 0 (every batch made an LLM call) and
        # finite (not bleeding from concurrent batches into an inflated delta).
        for line in per_batch:
            m_llm_time = re.search(r"llm=(\d+\.\d+)s", line)
            assert m_llm_time, f"expected llm=Xs timing, got: {line}"
            # Sanity: a single mock-LLM call is fast — under a second easily.
            # If snapshot leaked, this would catch concurrent batches' LLM time too.
            assert float(m_llm_time.group(1)) < 5.0, f"llm timing implausibly large for a single mock-LLM call: {line}"
    finally:
        await memory.delete_bank(bank_id, request_context=request_context)


@pytest.mark.asyncio
@pytest.mark.memory_backend_incompatible
async def test_same_lane_apply_rechecks_create_dedup(memory: MemoryEngine, request_context):
    """Concurrent CREATE preparation must not duplicate an identical committed observation."""
    bank_id = f"test-lane-create-dedup-{uuid.uuid4().hex[:8]}"
    await memory.ensure_bank_profile(bank_id=bank_id, request_context=request_context)
    try:
        async with memory._pool.acquire() as conn:
            facts = [
                await _insert_memory(conn, bank_id, "Fact one", ["shared"], "shared"),
                await _insert_memory(conn, bank_id, "Fact two", ["shared"], "shared"),
            ]

        mock_llm = MockLLM(provider="mock", api_key="", base_url="", model="mock-model")

        def callback(messages, scope):
            if scope != "consolidation":
                return _ConsolidationBatchResponse()
            prompt = "\n".join(m.get("content", "") for m in messages if m.get("role") == "user")
            fact_id = re.search(r"\[([0-9a-f-]{36})\]", prompt).group(1)
            return _ConsolidationBatchResponse(
                creates=[_CreateAction(text="The same shared observation", source_fact_ids=[fact_id])]
            )

        mock_llm.set_response_callback(callback)
        wrapper = MagicMock()
        wrapper.with_config.return_value = mock_llm
        original_llm = memory._consolidation_llm_config
        memory._consolidation_llm_config = wrapper
        try:
            with (
                _override_config(
                    memory,
                    consolidation_llm_parallelism=2,
                    consolidation_lane_llm_parallelism=2,
                    consolidation_llm_batch_size=1,
                    consolidation_dedup_threshold=0.97,
                ),
                patch.object(memory, "submit_async_consolidation"),
            ):
                result = await run_consolidation_job(
                    memory_engine=memory, bank_id=bank_id, request_context=request_context
                )
        finally:
            memory._consolidation_llm_config = original_llm

        assert result["status"] == "completed"
        async with memory._pool.acquire() as conn:
            observations = await conn.fetch(
                "SELECT text, source_memory_ids FROM memory_units WHERE bank_id=$1 AND fact_type='observation'",
                bank_id,
            )
        assert len(observations) == 1
        assert set(observations[0]["source_memory_ids"]) == set(facts)
    finally:
        await memory.delete_bank(bank_id, request_context=request_context)


@pytest.mark.asyncio
@pytest.mark.memory_backend_incompatible
@pytest.mark.parametrize("lane_parallelism", [1, 2])
async def test_intra_response_near_twin_creates_match_default_lane(
    memory: MemoryEngine, request_context, lane_parallelism: int
):
    """A batch's own second near-twin is not a stale predecessor requiring futile retries."""
    from hindsight_api.engine.consolidation import consolidator as mod

    bank_id = f"test-lane-own-twin-{uuid.uuid4().hex[:8]}"
    await memory.ensure_bank_profile(bank_id=bank_id, request_context=request_context)
    try:
        async with memory._pool.acquire() as conn:
            facts = [await _insert_memory(conn, bank_id, f"Paris source {i}", ["shared"], None) for i in range(2)]
        mock_llm = MockLLM(provider="mock", api_key="", base_url="", model="mock-model")
        calls = 0

        def callback(messages, scope):
            nonlocal calls
            assert scope == "consolidation"
            calls += 1
            return _ConsolidationBatchResponse(
                creates=[
                    _CreateAction(text="Alice lives in Paris.", source_fact_ids=[str(facts[0])]),
                    _CreateAction(text="Alice currently lives in Paris.", source_fact_ids=[str(facts[1])]),
                ]
            )

        mock_llm.set_response_callback(callback)
        wrapper = MagicMock()
        wrapper.with_config.return_value = mock_llm
        original_embed = mod._embed_observation_text
        embedding = None

        async def same_embedding(*args, **kwargs):
            nonlocal embedding
            if embedding is None:
                embedding = await original_embed(*args, **kwargs)
            return embedding

        with (
            patch.object(memory, "_consolidation_llm_config", wrapper),
            patch.object(memory, "submit_async_consolidation"),
            patch.object(mod, "_embed_observation_text", same_embedding),
            _override_config(
                memory,
                consolidation_llm_parallelism=2,
                consolidation_lane_llm_parallelism=lane_parallelism,
                consolidation_llm_batch_size=2,
                consolidation_dedup_threshold=0.8,
            ),
        ):
            result = await asyncio.wait_for(
                run_consolidation_job(memory_engine=memory, bank_id=bank_id, request_context=request_context), 15
            )
        assert result["status"] == "completed"
        assert calls == 1
        async with memory._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT text FROM memory_units WHERE bank_id=$1 AND fact_type='observation'", bank_id
            )
            states = await conn.fetch("SELECT consolidated_at FROM memory_units WHERE id=ANY($1::uuid[])", facts)
        assert {row["text"] for row in rows} == {"Alice lives in Paris.", "Alice currently lives in Paris."}
        assert all(row["consolidated_at"] is not None for row in states)
    finally:
        await memory.delete_bank(bank_id, request_context=request_context)


@pytest.mark.asyncio
@pytest.mark.memory_backend_incompatible
async def test_same_lane_apply_rechecks_observation_capacity(memory: MemoryEngine, request_context):
    """Concurrent CREATE preparation must never exceed a scope's observation cap."""
    bank_id = f"test-lane-capacity-{uuid.uuid4().hex[:8]}"
    await memory.ensure_bank_profile(bank_id=bank_id, request_context=request_context)
    try:
        async with memory._pool.acquire() as conn:
            facts = [
                await _insert_memory(conn, bank_id, "Capacity fact one", ["shared"], None),
                await _insert_memory(conn, bank_id, "Capacity fact two", ["shared"], None),
            ]

        wrapper, _ = _mock_llm_one_obs_per_fact()
        original_llm = memory._consolidation_llm_config
        memory._consolidation_llm_config = wrapper
        try:
            with (
                _override_config(
                    memory,
                    consolidation_llm_parallelism=2,
                    consolidation_lane_llm_parallelism=2,
                    consolidation_llm_batch_size=1,
                    consolidation_dedup_threshold=1.0,
                    max_observations_per_scope=1,
                ),
                patch.object(memory, "submit_async_consolidation"),
            ):
                result = await run_consolidation_job(
                    memory_engine=memory, bank_id=bank_id, request_context=request_context
                )
        finally:
            memory._consolidation_llm_config = original_llm

        assert result["status"] == "completed"
        async with memory._pool.acquire() as conn:
            observation_count = await conn.fetchval(
                "SELECT count(*) FROM memory_units WHERE bank_id=$1 AND fact_type='observation' AND tags @> ARRAY['shared']::varchar[]",
                bank_id,
            )
            states = await conn.fetch("SELECT consolidated_at FROM memory_units WHERE id=ANY($1::uuid[])", facts)
        assert observation_count == 1
        assert all(row["consolidated_at"] is not None for row in states)
    finally:
        await memory.delete_bank(bank_id, request_context=request_context)


@pytest.mark.asyncio
@pytest.mark.memory_backend_incompatible
async def test_default_lane_does_not_validate_recalled_targets(memory: MemoryEngine, request_context):
    """The lane=1 path retains base behavior: no SQL stale check or retries."""
    from hindsight_api.engine.consolidation import consolidator as mod

    bank_id = f"test-default-lane-{uuid.uuid4().hex[:8]}"
    await memory.ensure_bank_profile(bank_id=bank_id, request_context=request_context)
    try:
        async with memory._pool.acquire() as conn:
            fact = await _insert_memory(conn, bank_id, "Default update fact", ["default"], None)
            obs_id = uuid.uuid4()
            await conn.execute(
                "INSERT INTO memory_units (id, bank_id, text, fact_type, tags, source_memory_ids, created_at) "
                "VALUES ($1,$2,'Original','observation',$3,$4,now())",
                obs_id,
                bank_id,
                ["default"],
                [str(fact)],
            )

        async def recalled(**kwargs):
            return RecallResult.model_construct(
                results=[
                    MemoryFact.model_construct(
                        id=str(obs_id),
                        text="Outdated recalled text",
                        fact_type="observation",
                        tags=["default"],
                        source_fact_ids=[str(fact)],
                    )
                ]
            )

        mock_llm = MockLLM(provider="mock", api_key="", base_url="", model="mock-model")
        calls = 0

        def callback(messages, scope):
            nonlocal calls
            if scope != "consolidation":
                return _ConsolidationBatchResponse()
            calls += 1
            return _ConsolidationBatchResponse(
                updates=[
                    _UpdateAction(
                        observation_id=str(obs_id),
                        text="Updated",
                        source_fact_ids=[str(fact)],
                    )
                ]
            )

        mock_llm.set_response_callback(callback)
        wrapper = MagicMock()
        wrapper.with_config.return_value = mock_llm
        original_llm = memory._consolidation_llm_config
        memory._consolidation_llm_config = wrapper
        try:
            with (
                _override_config(memory, consolidation_lane_llm_parallelism=1, consolidation_dedup_threshold=1.0),
                patch.object(memory, "submit_async_consolidation"),
                patch.object(mod, "_find_related_observations", recalled),
            ):
                result = await run_consolidation_job(
                    memory_engine=memory, bank_id=bank_id, request_context=request_context
                )
        finally:
            memory._consolidation_llm_config = original_llm
        assert result["status"] == "completed" and calls == 1
        async with memory._pool.acquire() as conn:
            row = await conn.fetchrow("SELECT text, source_memory_ids FROM memory_units WHERE id=$1", obs_id)
        assert row["text"] == "Updated"
        assert str(fact) in [str(source_id) for source_id in row["source_memory_ids"]]
    finally:
        await memory.delete_bank(bank_id, request_context=request_context)


def test_oracle_connection_forces_serial_lane_apply(caplog):
    from hindsight_api.engine.consolidation import consolidator as mod

    with (
        patch.object(mod.get_memories(), "store_owned_for") as store_owned,
        caplog.at_level(logging.WARNING, logger=mod.__name__),
    ):
        assert _effective_lane_parallelism(4, MagicMock(backend_type="oracle"), "bank") == 1
        assert _effective_lane_parallelism(1, MagicMock(backend_type="oracle"), "bank") == 1
        store_owned.assert_not_called()
    assert "lane apply requires PostgreSQL" in caplog.text


@pytest.mark.asyncio
@pytest.mark.memory_backend_incompatible
async def test_store_owned_bank_disables_lane_parallelism(memory: MemoryEngine, request_context, caplog):
    """Store-owned observations never pass the SQL-only lane reference validator."""
    from hindsight_api.engine.consolidation import consolidator as mod

    bank_id = f"test-store-lane-{uuid.uuid4().hex[:8]}"
    await memory.ensure_bank_profile(bank_id=bank_id, request_context=request_context)
    try:
        async with memory._pool.acquire() as conn:
            await _insert_memory(conn, bank_id, "Store fact one", ["store"], None)
            await _insert_memory(conn, bank_id, "Store fact two", ["store"], None)
        wrapper, _ = _mock_llm_one_obs_per_fact()
        original_llm = memory._consolidation_llm_config
        memory._consolidation_llm_config = wrapper
        store = mod.get_memories()
        original_owned = store.store_owned_for
        owned_checks = 0

        def owned_at_dispatch(bank):
            nonlocal owned_checks
            owned_checks += 1
            # The actual Postgres store is SQL-backed. Simulate ownership only at
            # dispatch, and retain its real SQL-backed behavior for reads/writes.
            return owned_checks == 1 or original_owned(bank)

        try:
            with (
                _override_config(memory, consolidation_lane_llm_parallelism=4, consolidation_llm_batch_size=1),
                patch.object(memory, "submit_async_consolidation"),
                patch.object(store, "store_owned_for", side_effect=owned_at_dispatch),
                caplog.at_level(logging.WARNING, logger=mod.__name__),
            ):
                result = await run_consolidation_job(
                    memory_engine=memory, bank_id=bank_id, request_context=request_context
                )
        finally:
            memory._consolidation_llm_config = original_llm
        assert result["status"] == "completed"
        assert "observations are store-owned" in caplog.text
        assert "stale prepared reference" not in caplog.text
    finally:
        await memory.delete_bank(bank_id, request_context=request_context)


@pytest.mark.asyncio
@pytest.mark.memory_backend_incompatible
async def test_disjoint_scopes_run_concurrently(memory: MemoryEngine, request_context):
    """When write-scope sets are pairwise disjoint, the dispatcher must let
    groups run in parallel — we should observe simultaneous in-flight recalls
    on *different* scopes."""
    bank_id = f"test-disjoint-{uuid.uuid4().hex[:8]}"
    await memory.ensure_bank_profile(bank_id=bank_id, request_context=request_context)

    from hindsight_api.engine.consolidation import consolidator as consolidator_mod

    distinct_concurrent_scopes_seen = 0
    in_flight_scopes: set[frozenset[str]] = set()
    sample_lock = asyncio.Lock()
    all_scopes_entered = asyncio.Event()
    orig_find = consolidator_mod._find_related_observations

    async def tracked_find(*, memory_engine, bank_id, query, request_context, tags=None, config=None):
        nonlocal distinct_concurrent_scopes_seen
        scope = frozenset(tags or [])
        async with sample_lock:
            in_flight_scopes.add(scope)
            if len(in_flight_scopes) > distinct_concurrent_scopes_seen:
                distinct_concurrent_scopes_seen = len(in_flight_scopes)
            if len(in_flight_scopes) == 3:
                all_scopes_entered.set()
        try:
            await asyncio.wait_for(all_scopes_entered.wait(), timeout=10)
            return await orig_find(
                memory_engine=memory_engine,
                bank_id=bank_id,
                query=query,
                request_context=request_context,
                tags=tags,
                config=config,
            )
        finally:
            async with sample_lock:
                in_flight_scopes.discard(scope)

    try:
        async with memory._pool.acquire() as conn:
            await _insert_memory(conn, bank_id, "Alice memory", ["alice"], None)
            await _insert_memory(conn, bank_id, "Bob memory", ["bob"], None)
            await _insert_memory(conn, bank_id, "Carol memory", ["carol"], None)

        wrapper, _ = _mock_llm_one_obs_per_fact()
        original_llm = memory._consolidation_llm_config
        memory._consolidation_llm_config = wrapper
        try:
            with (
                _override_config(memory, consolidation_llm_parallelism=3, consolidation_llm_batch_size=1),
                patch.object(memory, "submit_async_consolidation"),
                patch.object(consolidator_mod, "_find_related_observations", tracked_find),
            ):
                result = await run_consolidation_job(
                    memory_engine=memory, bank_id=bank_id, request_context=request_context
                )
        finally:
            memory._consolidation_llm_config = original_llm

        assert result["status"] == "completed"
        # Three disjoint scopes + parallelism=3 → at some moment we should
        # see at least 2 distinct in-flight scopes.
        assert distinct_concurrent_scopes_seen >= 2, (
            f"expected concurrent in-flight recalls across disjoint scopes, "
            f"max observed = {distinct_concurrent_scopes_seen}"
        )
    finally:
        await memory.delete_bank(bank_id, request_context=request_context)


# ---------------------------------------------------------------------------
# Internal helper — sort lists of frozensets by their key for stable equality
# ---------------------------------------------------------------------------


def _ag_sorted(scopes):
    return sorted(scopes, key=lambda s: tuple(sorted(s)))
