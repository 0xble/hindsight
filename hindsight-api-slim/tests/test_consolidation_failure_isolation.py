"""Failure isolation for the consolidation dispatcher.

A DB failure inside one tag group's recall aborts the consolidation operation,
which the worker retries with a 5s base backoff. Plain ``asyncio.gather`` does
not cancel the sibling groups when it re-raises, so before this was fixed those
groups kept running detached — still calling the LLM, still stamping
``mark_consolidated`` — while the retry was already
under way. The per-scope ``scope_locks`` are local to one dispatch, so nothing
serialised an orphan against the retry and two consolidators could write the
same observation scope concurrently.

These tests pin:

1. ``_gather_or_cancel`` cancels and awaits its siblings, and re-raises the
   ORIGINAL exception (not an ``ExceptionGroup``) so the worker's
   ``_is_non_retryable_task_error`` classification still works.
2. End to end: a failing recall in one tag group cancels the other groups
   before they write, and the job does not wait for them.
"""

from __future__ import annotations

import asyncio
import json
import re
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import asyncpg
import pytest

from hindsight_api.config import _get_raw_config
from hindsight_api.engine.consolidation import consolidator as consolidator_module
from hindsight_api.engine.consolidation.consolidator import (
    _ConsolidationBatchResponse,
    _CreateAction,
    _gather_or_cancel,
    run_consolidation_job,
)
from hindsight_api.engine.llm_attempt_limit import CompletionAttemptLimitError
from hindsight_api.engine.llm_interface import OutputTooLongError
from hindsight_api.engine.llm_wrapper import LLMProvider
from hindsight_api.engine.memory_engine import (
    MemoryEngine,
    MentalModelRefreshError,
    _is_non_retryable_task_error,
)
from hindsight_api.engine.providers.mock_llm import MockLLM
from hindsight_api.engine.response_models import MemoryFact, RecallResult


class _RecallTimeout(Exception):
    """Stands in for a database command timeout raised inside a recall."""


# ---------------------------------------------------------------------------
# _gather_or_cancel unit tests (no database)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_gather_or_cancel_returns_results_in_order():
    async def one(value: int) -> int:
        await asyncio.sleep(0)
        return value

    assert await _gather_or_cancel([one(1), one(2), one(3)]) == [1, 2, 3]


@pytest.mark.asyncio
async def test_gather_or_cancel_cancels_siblings_before_returning():
    """The sibling must be cancelled AND awaited before the exception surfaces.

    ``sibling_finished`` would be True under plain ``asyncio.gather``, which
    leaves the sibling running past the raise.
    """
    started = asyncio.Event()
    sibling_finished = False
    sibling_cancelled = False

    async def sibling():
        nonlocal sibling_finished, sibling_cancelled
        started.set()
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            sibling_cancelled = True
            raise
        sibling_finished = True

    async def boom():
        await started.wait()
        raise _RecallTimeout("simulated DB command timeout")

    with pytest.raises(_RecallTimeout):
        await _gather_or_cancel([boom(), sibling()])

    assert sibling_cancelled is True
    assert sibling_finished is False


@pytest.mark.asyncio
async def test_gather_or_cancel_reraises_original_exception_unwrapped():
    """An ExceptionGroup here (i.e. asyncio.TaskGroup) would break retry
    classification: the worker isinstance-checks the raised exception, so a
    wrapped integrity violation would be retried forever instead of dropped."""

    async def boom():
        raise asyncpg.exceptions.UniqueViolationError("duplicate key")

    async def idle():
        await asyncio.sleep(30)

    with pytest.raises(asyncpg.exceptions.UniqueViolationError) as exc_info:
        await _gather_or_cancel([boom(), idle()])

    assert not isinstance(exc_info.value, BaseExceptionGroup)
    assert _is_non_retryable_task_error(exc_info.value) is True


def test_mental_model_refresh_failure_is_not_retried():
    """A guarded refresh failure preserves content and must not occupy a retry slot."""
    error = MentalModelRefreshError(
        "delta operations did not reach the document",
        outcome="refresh_failed_delta_not_applied",
        reason="delta_not_applied",
    )
    assert _is_non_retryable_task_error(error) is True


# ---------------------------------------------------------------------------
# End-to-end fixtures + helpers
# ---------------------------------------------------------------------------


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


def _mock_llm_one_obs_per_fact():
    """MockLLM wrapper emitting one CREATE per fact id found in the prompt."""
    mock_llm = MockLLM(provider="mock", api_key="", base_url="", model="mock-model")

    def callback(messages, scope):
        if scope != "consolidation":
            return _ConsolidationBatchResponse()
        prompt = "\n".join(m.get("content", "") for m in messages if m.get("role") == "user")
        fact_ids = re.findall(r"\[([0-9a-f-]{36})\]", prompt)
        creates = [_CreateAction(text=f"Observation about fact {fid[:8]}", source_fact_ids=[fid]) for fid in fact_ids]
        return _ConsolidationBatchResponse(creates=creates)

    mock_llm.set_response_callback(callback)
    wrapper = MagicMock()
    wrapper.with_config.return_value = mock_llm
    return wrapper


async def _insert_memory(conn, bank_id: str, text: str, tags: list[str]) -> uuid.UUID:
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
        json.dumps(None),
    )
    return mem_id


async def _count_observations(memory: MemoryEngine, bank_id: str, request_context) -> int:
    return (
        await memory.list_memory_units(bank_id, fact_type="observation", limit=1000, request_context=request_context)
    )["total"]


# ---------------------------------------------------------------------------
# End-to-end tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.memory_backend_incompatible
async def test_recall_failure_cancels_sibling_tag_groups(memory: MemoryEngine, request_context):
    """One group's recall times out → the other two groups are cancelled before
    they write, and the job propagates the original error without waiting for
    them."""
    bank_id = f"test-cancel-{uuid.uuid4().hex[:8]}"
    await memory.ensure_bank_profile(bank_id=bank_id, request_context=request_context)
    try:
        async with memory._pool.acquire() as conn:
            await _insert_memory(conn, bank_id, "Alice likes tea", ["boom"])
            await _insert_memory(conn, bank_id, "Bob bikes daily", ["user:bob"])
            await _insert_memory(conn, bank_id, "Carol reads books", ["user:carol"])

        siblings_parked = asyncio.Event()
        parked_count = 0
        cancelled_scopes: list[frozenset[str]] = []

        async def fake_recall(*, tags=None, **_kwargs):
            nonlocal parked_count
            tag_set = frozenset(tags or [])
            if "boom" in tag_set:
                # Fail only once both siblings are parked, so the assertion below
                # is about cancellation rather than about which group won a race.
                await siblings_parked.wait()
                raise _RecallTimeout("simulated DB command timeout")
            parked_count += 1
            if parked_count >= 2:
                siblings_parked.set()
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                cancelled_scopes.append(tag_set)
                raise
            return RecallResult(results=[])

        original_llm = memory._consolidation_llm_config
        memory._consolidation_llm_config = _mock_llm_one_obs_per_fact()
        try:
            with (
                _override_config(memory, consolidation_llm_parallelism=3, consolidation_llm_batch_size=1),
                patch.object(memory, "submit_async_consolidation"),
                patch.object(consolidator_module, "_find_related_observations", fake_recall),
            ):
                started = asyncio.get_running_loop().time()
                with pytest.raises(_RecallTimeout):
                    await run_consolidation_job(memory_engine=memory, bank_id=bank_id, request_context=request_context)
                elapsed = asyncio.get_running_loop().time() - started
        finally:
            memory._consolidation_llm_config = original_llm

        # Both sibling groups were cancelled, not left running detached.
        assert sorted(cancelled_scopes, key=sorted) == [frozenset({"user:bob"}), frozenset({"user:carol"})]
        # And the job did not block on their 30s sleep.
        assert elapsed < 10

        # Nothing was written: the cancelled groups never reached their commit,
        # and no orphan lands a write after the operation has already failed.
        await asyncio.sleep(0.2)
        assert await _count_observations(memory, bank_id, request_context) == 0
    finally:
        await memory.delete_bank(bank_id, request_context=request_context)


@pytest.mark.asyncio
@pytest.mark.memory_backend_incompatible
@pytest.mark.parametrize("failure", ["round_budget", "output_too_long", "attempt_limit"])
async def test_round_budget_defers_facts_but_input_failure_still_marks_failed(memory, request_context, failure):
    """Drive provider attempt guards through a real round and inspect durable fact state."""
    bank_id = f"test-budget-defer-{uuid.uuid4().hex[:8]}"
    await memory.ensure_bank_profile(bank_id=bank_id, request_context=request_context)
    llm = LLMProvider(provider="openai", api_key="synthetic", base_url="https://example.invalid", model="stub")
    await llm._provider_impl._client.close()
    requests = []

    async def complete(**kwargs):
        requests.append(kwargs)
        if failure == "output_too_long":
            raise OutputTooLongError("synthetic single-fact output limit")
        if failure == "attempt_limit":
            raise CompletionAttemptLimitError("synthetic per-call attempt limit")
        prompt = kwargs["messages"][-1]["content"]
        fact_ids = re.findall(r"\[([0-9a-f-]{36})\]", prompt)
        response = (
            {"creates": [{"text": f"Observation about {fact_ids[0]}", "source_fact_ids": fact_ids}]}
            if "Return a COMPLETE replacement" in prompt
            else {"creates": [{}]}
        )
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(response)), finish_reason="stop")],
            usage=SimpleNamespace(
                prompt_tokens=10,
                completion_tokens=12,
                total_tokens=22,
                prompt_tokens_details=None,
                completion_tokens_details=None,
            ),
        )

    llm._provider_impl._client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=complete)))
    original_llm = memory._consolidation_llm_config
    memory._consolidation_llm_config = SimpleNamespace(with_config=lambda *args, **kwargs: llm)
    fact_count = 4 if failure == "round_budget" else 1
    try:
        async with memory._pool.acquire() as conn:
            ids = [await _insert_memory(conn, bank_id, f"Synthetic fact {index}", []) for index in range(fact_count)]
        with (
            _override_config(
                memory,
                consolidation_max_memories_per_round=100,  # One shared correction credit.
                consolidation_batch_size=2,  # More than one fetch, including deferred ids.
                consolidation_llm_batch_size=2,
                consolidation_llm_parallelism=2,
                consolidation_lane_llm_parallelism=2,
                consolidation_dedup_threshold=1.0,
                llm_language_integrity="off",
            ),
            patch.object(
                consolidator_module, "_find_related_observations", AsyncMock(return_value=RecallResult(results=[]))
            ),
            patch.object(memory, "submit_async_consolidation"),
        ):
            result = await asyncio.wait_for(run_consolidation_job(memory, bank_id, request_context), timeout=20)
            # Inspect both durable stamps together; the public read API exposes
            # a derived state, not the underlying NULL/failed marker invariant.
            async with memory._pool.acquire() as conn:
                rows = await conn.fetch(
                    "SELECT id, consolidated_at, consolidation_failed_at FROM memory_units WHERE id = ANY($1)", ids
                )
            assert result["status"] == "completed"
            if failure != "round_budget":
                assert len(requests) == 1
                assert rows[0]["consolidated_at"] is None
                assert rows[0]["consolidation_failed_at"] is not None
                assert result["memories_failed"] == 1
                assert result["memories_deferred"] == 0
            else:
                assert all(row["consolidation_failed_at"] is None for row in rows)
                pending_ids = [row["id"] for row in rows if row["consolidated_at"] is None]
                assert len(pending_ids) == 2
                assert result["memories_failed"] == 0
                assert result["memories_deferred"] == len(pending_ids)
                assert result["schema_correction_attempts"] == 1
                assert result["schema_correction_budget_exhausted"] == 1
                # No bisection or re-fetch churn for the deferred two-fact batch.
                assert len(requests) == 3
                # A fresh round may use a new credit and consolidate the same facts.
                next_result = await asyncio.wait_for(
                    run_consolidation_job(memory, bank_id, request_context), timeout=20
                )
                assert next_result["memories_failed"] == next_result["memories_deferred"] == 0
                async with memory._pool.acquire() as conn:
                    assert (
                        await conn.fetchval(
                            "SELECT count(*) FROM memory_units WHERE id = ANY($1) "
                            "AND consolidated_at IS NOT NULL AND consolidation_failed_at IS NULL",
                            pending_ids,
                        )
                        == 2
                    )
    finally:
        memory._consolidation_llm_config = original_llm
        await memory.delete_bank(bank_id, request_context=request_context)


def _sdk_reply(value):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(value)), finish_reason="stop")],
        usage=SimpleNamespace(
            prompt_tokens=10,
            completion_tokens=12,
            total_tokens=22,
            prompt_tokens_details=None,
            completion_tokens_details=None,
        ),
    )


@pytest.mark.asyncio
@pytest.mark.memory_backend_incompatible
async def test_round_budget_multiscope_defer_keeps_committed_action_accounting(memory, request_context):
    bank = f"test-budget-multiscope-{uuid.uuid4().hex[:8]}"
    await memory.ensure_bank_profile(bank_id=bank, request_context=request_context)
    llm = LLMProvider(provider="openai", api_key="synthetic", base_url="https://example.invalid", model="stub")
    await llm._provider_impl._client.close()
    calls = []
    old_obs_id = uuid.uuid4()

    async def complete(**kwargs):
        calls.append(kwargs)
        prompt = kwargs["messages"][-1]["content"]
        ids = re.findall(r"\[([0-9a-f-]{36})\]", prompt)
        reply = (
            {
                "creates": [{"text": "Independent observation", "source_fact_ids": ids}],
                "deletes": [{"observation_id": str(old_obs_id)}],
            }
            if "Return a COMPLETE replacement" in prompt
            else {"creates": [{}]}
        )
        return _sdk_reply(reply)

    llm._provider_impl._client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=complete)))
    original = memory._consolidation_llm_config
    memory._consolidation_llm_config = SimpleNamespace(with_config=lambda *args, **kwargs: llm)
    try:
        async with memory._pool.acquire() as conn:
            fact_id = await _insert_memory(conn, bank, "Independent fact", ["a", "b"])
            await conn.execute(
                "UPDATE memory_units SET observation_scopes=$1::jsonb WHERE id=$2", json.dumps([["a"], ["b"]]), fact_id
            )
            await conn.execute(
                "INSERT INTO memory_units (id, bank_id, text, fact_type, tags, created_at) "
                "VALUES ($1, $2, 'Independent prior observation', 'observation', ARRAY['a'], now())",
                old_obs_id,
                bank,
            )
        related = RecallResult(
            results=[
                MemoryFact(
                    id=str(old_obs_id), text="Independent prior observation", fact_type="observation", tags=["a"]
                )
            ]
        )
        with (
            _override_config(
                memory,
                enable_observations=True,
                consolidation_max_memories_per_round=100,
                consolidation_batch_size=2,
                consolidation_llm_batch_size=1,
                consolidation_llm_parallelism=2,
                consolidation_lane_llm_parallelism=2,
                consolidation_dedup_threshold=1.0,
                llm_language_integrity="off",
            ),
            patch.object(consolidator_module, "_find_related_observations", AsyncMock(return_value=related)),
            patch.object(memory, "submit_async_consolidation", AsyncMock()),
        ):
            result = await asyncio.wait_for(
                consolidator_module.run_consolidation_job(memory, bank, request_context), 20
            )
            # Internal NULL stamps distinguish resource deferral from durable
            # failure even though an earlier scope has committed observations.
            async with memory._pool.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT consolidated_at, consolidation_failed_at FROM memory_units WHERE id=$1", fact_id
                )
            observations = await memory.list_memory_units(
                bank, fact_type="observation", limit=100, request_context=request_context
            )
            actual_created = observations["total"]
            actual_deleted = all(item["id"] != str(old_obs_id) for item in observations["items"])
            assert actual_created == 1 and actual_deleted
            # Earlier scope committed even though the later scope was deferred.
            assert result["observations_deleted"] == int(actual_deleted)
            assert result["observations_created"] == actual_created
            assert result["actions_executed"] == actual_created
            assert row["consolidated_at"] is None and row["consolidation_failed_at"] is None
            assert result["memories_processed"] == result["memories_failed"] == 0
            assert result["memories_deferred"] == 1
            assert result["schema_correction_attempts"] == result["schema_correction_budget_exhausted"] == 1
            assert len(calls) == 3
    finally:
        memory._consolidation_llm_config = original
        await memory.delete_bank(bank, request_context=request_context)


@pytest.mark.asyncio
@pytest.mark.memory_backend_incompatible
@pytest.mark.parametrize("fair", [False, True])
async def test_round_budget_parallel_lane_drains_and_refetch_excludes_deferred(memory, request_context, fair):
    bank = f"test-budget-lane-{uuid.uuid4().hex[:8]}"
    await memory.ensure_bank_profile(bank_id=bank, request_context=request_context)
    llm = LLMProvider(provider="openai", api_key="synthetic", base_url="https://example.invalid", model="stub")
    await llm._provider_impl._client.close()
    calls = []

    async def complete(**kwargs):
        calls.append(kwargs)
        prompt = kwargs["messages"][-1]["content"]
        ids = re.findall(r"\[([0-9a-f-]{36})\]", prompt)
        await asyncio.sleep(0.01)
        reply = (
            {"creates": [{"text": f"Observation {ids[0]}", "source_fact_ids": ids}]}
            if "Return a COMPLETE replacement" in prompt
            else {"creates": [{}]}
        )
        return _sdk_reply(reply)

    llm._provider_impl._client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=complete)))
    original = memory._consolidation_llm_config
    memory._consolidation_llm_config = SimpleNamespace(with_config=lambda *args, **kwargs: llm)
    try:
        async with memory._pool.acquire() as conn:
            ids = [await _insert_memory(conn, bank, f"Lane fact {index}", []) for index in range(6)]
        with (
            _override_config(
                memory,
                enable_observations=True,
                consolidation_max_memories_per_round=100,
                consolidation_batch_size=6,
                consolidation_llm_batch_size=1,
                consolidation_llm_parallelism=3,
                consolidation_lane_llm_parallelism=3,
                consolidation_fair_group_selection=fair,
                consolidation_dedup_threshold=1.0,
                llm_language_integrity="off",
            ),
            patch.object(
                consolidator_module, "_find_related_observations", AsyncMock(return_value=RecallResult(results=[]))
            ),
            patch.object(memory, "submit_async_consolidation", AsyncMock()),
        ):
            results = []
            for round_index in range(6):
                results.append(
                    await asyncio.wait_for(consolidator_module.run_consolidation_job(memory, bank, request_context), 20)
                )
                # Neither failed nor consolidated stamps may land on deferred
                # facts; inspect both internal columns on every fresh round.
                async with memory._pool.acquire() as conn:
                    rows = await conn.fetch(
                        "SELECT consolidated_at, consolidation_failed_at FROM memory_units WHERE id=ANY($1)", ids
                    )
                completed = sum(row["consolidated_at"] is not None for row in rows)
                failed = sum(row["consolidation_failed_at"] is not None for row in rows)
                assert results[-1]["memories_deferred"] == 5 - round_index
                assert results[-1]["memories_failed"] == 0
                assert results[-1]["memories_processed"] == 1
                assert failed == 0
                assert completed == round_index + 1
                assert results[-1]["schema_correction_attempts"] == 1
                assert results[-1]["schema_correction_budget_exhausted"] == 5 - round_index
            # Six, then five ... then one initial call, plus one correction per round.
            assert len(calls) == 27
    finally:
        memory._consolidation_llm_config = original
        await memory.delete_bank(bank, request_context=request_context)


@pytest.mark.asyncio
@pytest.mark.memory_backend_incompatible
async def test_round_budget_first_scope_defer_does_not_recount_previous_leaf(memory, request_context):
    """Bisect one batch: a successful leaf must not leak its results into the next."""
    bank = f"test-budget-leaves-{uuid.uuid4().hex[:8]}"
    await memory.ensure_bank_profile(bank_id=bank, request_context=request_context)
    llm = LLMProvider(provider="openai", api_key="synthetic", base_url="https://example.invalid", model="stub")
    await llm._provider_impl._client.close()
    calls = []
    first_leaf = None

    async def complete(**kwargs):
        nonlocal first_leaf
        calls.append(kwargs)
        prompt = kwargs["messages"][-1]["content"]
        ids = re.findall(r"\[([0-9a-f-]{36})\]", prompt)
        if len(ids) > 1:
            first_leaf = ids[0]
            raise OutputTooLongError("synthetic batch-size failure")
        if ids[0] == first_leaf and ("Return a COMPLETE replacement" in prompt or len(calls) == 4):
            return _sdk_reply({"creates": [{"text": "Leaf observation", "source_fact_ids": ids}]})
        return _sdk_reply({"creates": [{}]})

    llm._provider_impl._client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=complete)))
    original = memory._consolidation_llm_config
    memory._consolidation_llm_config = SimpleNamespace(with_config=lambda *args, **kwargs: llm)
    try:
        async with memory._pool.acquire() as conn:
            ids = [await _insert_memory(conn, bank, f"Leaf fact {index}", ["a", "b"]) for index in range(2)]
            await conn.execute(
                "UPDATE memory_units SET observation_scopes=$1::jsonb WHERE id=ANY($2)",
                json.dumps([["a"], ["b"]]),
                ids,
            )
        with (
            _override_config(
                memory,
                consolidation_max_memories_per_round=100,
                consolidation_batch_size=2,
                consolidation_llm_batch_size=2,
                consolidation_llm_parallelism=1,
                consolidation_lane_llm_parallelism=1,
                consolidation_dedup_threshold=1.0,
                llm_language_integrity="off",
            ),
            patch.object(
                consolidator_module, "_find_related_observations", AsyncMock(return_value=RecallResult(results=[]))
            ),
            patch.object(memory, "submit_async_consolidation", AsyncMock()),
        ):
            result = await asyncio.wait_for(run_consolidation_job(memory, bank, request_context), 20)
        observations = await memory.list_memory_units(
            bank, fact_type="observation", limit=100, request_context=request_context
        )
        assert observations["total"] == result["observations_created"] == result["actions_executed"] == 2
        assert sorted(item["tags"] for item in observations["items"]) == [["a"], ["b"]]
        assert result["memories_processed"] == result["memories_deferred"] == 1
        assert result["memories_failed"] == 0
        # Both internal stamps must stay NULL for the deferred second leaf.
        async with memory._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT id, consolidated_at, consolidation_failed_at FROM memory_units WHERE id=ANY($1)", ids
            )
        assert all(row["consolidation_failed_at"] is None for row in rows)
        assert all((row["consolidated_at"] is not None) == (str(row["id"]) == first_leaf) for row in rows)
        assert result["schema_correction_attempts"] == result["schema_correction_budget_exhausted"] == 1
        assert len(calls) == 5
    finally:
        memory._consolidation_llm_config = original
        await memory.delete_bank(bank, request_context=request_context)
