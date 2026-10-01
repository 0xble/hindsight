"""Source coverage when a prepared exact fold misses or PostgreSQL folding is unavailable."""

import asyncio
import uuid
from dataclasses import replace
from unittest.mock import AsyncMock, patch

import pytest

from hindsight_api.config import _get_raw_config
from hindsight_api.engine.consolidation import consolidator as c
from hindsight_api.engine.response_models import MemoryFact, RecallResult
from tests.test_consolidation_scope_parallelism import _insert_memory, _mock_llm_one_obs_per_fact


@pytest.mark.asyncio
@pytest.mark.memory_backend_incompatible
async def test_exact_create_cas_miss_after_prior_create_fold_keeps_sources(memory, request_context):
    bank = "exact-fold-fallback-" + uuid.uuid4().hex[:8]
    await memory.ensure_bank_profile(bank, request_context=request_context)
    try:
        async with memory._pool.acquire() as conn:
            prior = await _insert_memory(conn, bank, "Prior source", [], "shared")
            first = await _insert_memory(conn, bank, "Semantic source", [], "shared")
            second = await _insert_memory(conn, bank, "Exact source", [], "shared")
            twin = uuid.uuid4()
            await conn.execute(
                "INSERT INTO memory_units (id,bank_id,text,fact_type,source_memory_ids) "
                "VALUES ($1,$2,'Original twin','observation',$3)",
                twin,
                bank,
                [prior],
            )
            facts = [
                dict(await conn.fetchrow("SELECT * FROM memory_units WHERE id=$1", fid)) for fid in (first, second)
            ]
        wrapper, mock = _mock_llm_one_obs_per_fact()
        mock.set_response_callback(
            lambda messages, scope: c._ConsolidationBatchResponse(
                creates=[
                    c._CreateAction(text="Semantic twin", source_fact_ids=[str(first)]),
                    c._CreateAction(text="Original twin", source_fact_ids=[str(second)]),
                ]
            )
        )
        merge = c._DedupOutcome(
            best_id=str(twin), best_text="Original twin", merged_text="Merged twin", should_merge=True
        )
        ready = asyncio.Event()
        ready.set()
        with (
            patch.object(
                c, "_find_related_observations", AsyncMock(return_value=RecallResult.model_construct(results=[]))
            ),
            patch.object(c, "_dedup_adjudicate", AsyncMock(side_effect=[merge, c._DedupOutcome(None, "", False)])),
            patch.object(c, "_dedup_probe", AsyncMock(return_value=merge)),
        ):
            results, _, failed = await c._process_memory_batch(
                pool=memory._backend,
                memory_engine=memory,
                llm_config=wrapper.with_config(),
                bank_id=bank,
                memories=facts,
                request_context=request_context,
                config=replace(_get_raw_config(), consolidation_dedup_threshold=0.9, llm_language_integrity="off"),
                obs_tags_override=[],
                mark_consolidated_ids=[first, second],
                apply_turn=(ready, asyncio.Event()),
            )
        assert not failed
        async with memory._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT text,source_memory_ids FROM memory_units WHERE bank_id=$1 AND fact_type='observation'", bank
            )
            stamps = await conn.fetch(
                "SELECT consolidated_at FROM memory_units WHERE id=ANY($1::uuid[])", [first, second]
            )
        # The first real fold rewrote the exact snapshot. The second CREATE must
        # fall back to insertion instead of stamping a source with no durable carrier.
        assert {(row["text"], frozenset(row["source_memory_ids"])) for row in rows} == {
            ("Merged twin", frozenset([prior, first])),
            ("Original twin", frozenset([second])),
        }
        assert all(row["consolidated_at"] is not None for row in stamps)
        assert [result["action"] for result in results] == ["created", "created"]
    finally:
        await memory.delete_bank(bank, request_context=request_context)


@pytest.mark.asyncio
@pytest.mark.memory_backend_incompatible
@pytest.mark.parametrize("target", ["shown", "reply"])
@pytest.mark.parametrize("threshold", [0.9, 1.0])
async def test_oracle_create_twins_never_enter_postgres_fold(memory, request_context, target, threshold):
    """Exercise preparation/apply on PG, substituting only the dialect policy.

    This is a routing regression, not live Oracle SQL execution. Both shown and
    response-update twins must preserve CREATE sources without calling PG-only SQL.
    """
    bank = "oracle-fold-routing-" + uuid.uuid4().hex[:8]
    await memory.ensure_bank_profile(bank, request_context=request_context)
    try:
        async with memory._pool.acquire() as conn:
            prior = await _insert_memory(conn, bank, "Prior source", [], "shared")
            fresh = await _insert_memory(conn, bank, "Fresh source", [], "shared")
            twin = uuid.uuid4()
            await conn.execute(
                "INSERT INTO memory_units (id,bank_id,text,fact_type,source_memory_ids) "
                "VALUES ($1,$2,'Original twin','observation',$3)",
                twin,
                bank,
                [prior],
            )
            fact = dict(await conn.fetchrow("SELECT * FROM memory_units WHERE id=$1", fresh))
        shown = MemoryFact.model_construct(
            id=str(twin), text="Original twin", fact_type="observation", tags=[], source_fact_ids=[str(prior)]
        )
        wrapper, mock = _mock_llm_one_obs_per_fact()
        mock.set_response_callback(
            lambda messages, scope: c._ConsolidationBatchResponse(
                creates=[
                    c._CreateAction(
                        text="Updated twin" if target == "reply" else "Original twin", source_fact_ids=[str(fresh)]
                    )
                ],
                updates=[c._UpdateAction(observation_id=str(twin), text="Updated twin", source_fact_ids=[str(fresh)])]
                if target == "reply"
                else [],
            )
        )
        with (
            patch.object(c, "get_config", return_value=replace(_get_raw_config(), database_backend="oracle")),
            patch.object(
                c, "_find_related_observations", AsyncMock(return_value=RecallResult.model_construct(results=[shown]))
            ),
            patch.object(c, "_apply_dedup_create_fold", wraps=c._apply_dedup_create_fold) as fold,
        ):
            await c._process_memory_batch(
                pool=memory._backend,
                memory_engine=memory,
                llm_config=wrapper.with_config(),
                bank_id=bank,
                memories=[fact],
                request_context=request_context,
                config=replace(
                    _get_raw_config(), consolidation_dedup_threshold=threshold, llm_language_integrity="off"
                ),
                obs_tags_override=[],
                mark_consolidated_ids=[fresh],
            )
        fold.assert_not_called()
        async with memory._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT source_memory_ids FROM memory_units WHERE bank_id=$1 AND fact_type='observation'", bank
            )
        assert len(rows) == 2
        assert any(row["source_memory_ids"] == [fresh] for row in rows)
    finally:
        await memory.delete_bank(bank, request_context=request_context)
