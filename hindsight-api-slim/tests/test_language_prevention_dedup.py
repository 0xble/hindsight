"""Real isolated PG write/readback at both final dedup persistence boundaries."""

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from hindsight_api.engine.chunk_ids import build_chunk_id
from hindsight_api.engine.consolidation import consolidator as c
from hindsight_api.engine.memories import get_memories
from hindsight_api.engine.response_models import LLMCallResult, MemoryFact, TokenUsage
from tests.test_consolidation_batch_atomicity import (
    _insert_memory,
    _llm,
    _observations,
    _override_config,
    _pending_facts,
)
from tests.test_language_prevention import ENGLISH, SPANISH


async def source_fact(conn, bank, text, *, consolidated=False):
    document = uuid.uuid4().hex
    chunk = build_chunk_id(bank, document, 0)
    await conn.execute(
        "INSERT INTO documents (id, bank_id, original_text, content_hash) VALUES ($1,$2,$3,'hash')",
        document,
        bank,
        text,
    )
    await conn.execute(
        "INSERT INTO chunks (chunk_id, document_id, bank_id, chunk_index, chunk_text) VALUES ($1,$2,$3,0,$4)",
        chunk,
        document,
        bank,
        text,
    )
    fact = await _insert_memory(conn, bank, text, [])
    await conn.execute(
        "UPDATE memory_units SET document_id=$1, chunk_id=$2, consolidated_at=CASE WHEN $4 THEN now() ELSE NULL END WHERE bank_id=$3 AND id=$5",
        document,
        chunk,
        bank,
        consolidated,
        fact,
    )
    return fact


async def observation(conn, bank, text, fact):
    obs = uuid.uuid4()
    await conn.execute(
        "INSERT INTO memory_units (id, bank_id, text, fact_type, source_memory_ids) VALUES ($1,$2,$3,'observation',$4)",
        obs,
        bank,
        text,
        [fact],
    )
    return obs


@pytest.mark.asyncio
@pytest.mark.memory_backend_incompatible
@pytest.mark.parametrize("action", ["create", "update"])
@pytest.mark.parametrize("corrected", [False, True])
async def test_dedup_final_text_is_checked_before_any_write(memory, request_context, action, corrected):
    bank = "language-dedup-" + uuid.uuid4().hex[:8]
    await memory.get_bank_profile(bank, request_context=request_context)
    try:
        async with memory._pool.acquire() as conn:
            fresh = await source_fact(conn, bank, ENGLISH)
            prior = await source_fact(conn, bank, ENGLISH, consolidated=True)
            twin = await observation(conn, bank, "The original review has been recorded for future reference.", prior)
            anchor = await observation(conn, bank, "The team is preparing for the next review.", prior)
        calls = []

        def response(messages, scope):
            calls.append(scope)
            if scope == "consolidation_dedup":
                return c._DedupDecision(
                    action="merge", text=ENGLISH if corrected and calls.count(scope) == 2 else SPANISH
                )
            if action == "create":
                return c._ConsolidationBatchResponse(
                    creates=[c._CreateAction(text=ENGLISH, source_fact_ids=[str(fresh)])],
                    deletes=[c._DeleteAction(observation_id=str(anchor))],
                )
            return c._ConsolidationBatchResponse(
                updates=[c._UpdateAction(observation_id=str(anchor), text=ENGLISH, source_fact_ids=[str(fresh)])]
            )

        recalled = SimpleNamespace(
            results=[
                MemoryFact(
                    id=str(anchor),
                    text="The team is preparing for the next review.",
                    fact_type="observation",
                    source_fact_ids=[str(prior)],
                )
            ],
            source_facts={},
        )
        with (
            patch.object(memory, "_consolidation_llm_config", _llm(response)),
            patch.object(memory, "submit_async_consolidation"),
            patch.object(c, "_find_related_observations", AsyncMock(return_value=recalled)),
            patch.object(
                get_memories(),
                "recall_unified",
                AsyncMock(
                    return_value={
                        "observation": SimpleNamespace(
                            semantic=[
                                SimpleNamespace(
                                    id=str(twin),
                                    text="The original review has been recorded for future reference.",
                                    similarity=1.0,
                                )
                            ]
                        )
                    }
                ),
            ),
            _override_config(
                memory,
                enable_observations=True,
                llm_language_integrity="reject",
                llm_output_language=None,
                consolidation_dedup_threshold=0.9,
                consolidation_llm_batch_size=1,
                consolidation_llm_parallelism=1,
            ),
        ):
            result = await c.run_consolidation_job(memory_engine=memory, bank_id=bank, request_context=request_context)
        assert calls == ["consolidation", "consolidation_dedup", "consolidation_dedup"]
        async with memory._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT text, consolidated_at, consolidation_failed_at FROM memory_units WHERE bank_id=$1 AND id=$2",
                bank,
                fresh,
            )
            assert row["text"] == ENGLISH
            assert (row["consolidation_failed_at"] is not None) is (not corrected)
            assert (row["consolidated_at"] is not None) is corrected
            assert (
                await conn.fetchval("SELECT count(*) FROM chunks WHERE bank_id=$1 AND chunk_text=$2", bank, ENGLISH)
                == 2
            )
            assert (
                await conn.fetchval(
                    "SELECT count(*) FROM documents WHERE bank_id=$1 AND original_text=$2", bank, ENGLISH
                )
                == 2
            )
        assert SPANISH not in await _observations(memory, bank)
        if corrected:
            assert ENGLISH in await _observations(memory, bank)
        else:
            assert result["memories_failed"] == 1
            assert await _observations(memory, bank) == sorted(
                [
                    "The original review has been recorded for future reference.",
                    "The team is preparing for the next review.",
                ]
            )
    finally:
        await memory.delete_bank(bank, request_context=request_context)


@pytest.mark.asyncio
@pytest.mark.memory_backend_incompatible
@pytest.mark.parametrize("missing", [False, True])
async def test_dedup_uses_twin_original_not_its_generated_language(memory, request_context, missing):
    import dataclasses

    from hindsight_api.config import _get_raw_config
    from hindsight_api.engine.language_integrity import GeneratedLanguageMismatch
    from tests.test_language_prevention import FRENCH

    bank = "language-lineage-" + uuid.uuid4().hex[:8]
    await memory.get_bank_profile(bank, request_context=request_context)
    try:
        async with memory._pool.acquire() as conn:
            fresh = await source_fact(conn, bank, ENGLISH)
            prior = await source_fact(conn, bank, FRENCH, consolidated=True)
            twin = await observation(conn, bank, "An old generated English observation.", prior)
            if missing:
                await conn.execute("DELETE FROM chunks WHERE bank_id=$1 AND chunk_text=$2", bank, FRENCH)
        generated = "Le rapport présente les conclusions de la réunion et les recommandations pour la prochaine étape."
        llm = SimpleNamespace(
            call=AsyncMock(
                return_value=LLMCallResult(content=c._DedupDecision(action="merge", text=generated), usage=TokenUsage())
            )
        )
        config = dataclasses.replace(
            _get_raw_config(),
            llm_language_integrity="reject",
            llm_output_language=None,
            consolidation_dedup_threshold=0.9,
        )
        with patch.object(
            get_memories(),
            "recall_unified",
            AsyncMock(
                return_value={
                    "observation": SimpleNamespace(
                        semantic=[
                            SimpleNamespace(id=str(twin), text="An old generated English observation.", similarity=1.0)
                        ]
                    )
                }
            ),
        ):
            kwargs = dict(
                pool=memory._pool,
                memory_engine=memory,
                bank_id=bank,
                config=config,
                dedup_llm_config=llm,
                anchor_text=ENGLISH,
                anchor_emb_str="[]",
                tags=[],
                exclude_id=None,
                anchor_source_ids=[str(fresh)],
            )
            if missing:
                with pytest.raises(GeneratedLanguageMismatch):
                    await c._dedup_adjudicate(**kwargs)
                assert llm.call.await_count == 2
            else:
                outcome = await c._dedup_adjudicate(**kwargs)
                assert outcome.should_merge and outcome.merged_text == generated
                assert llm.call.await_count == 1
    finally:
        await memory.delete_bank(bank, request_context=request_context)


@pytest.mark.asyncio
@pytest.mark.memory_backend_incompatible
@pytest.mark.parametrize("batch_size", [1, 2])
async def test_language_failure_is_held_and_later_healthy_work_drains(memory, request_context, batch_size):
    bank = "language-held-" + uuid.uuid4().hex[:8]
    await memory.get_bank_profile(bank, request_context=request_context)
    try:
        async with memory._pool.acquire() as conn:
            bad = await source_fact(conn, bank, ENGLISH)
            good = await source_fact(conn, bank, ENGLISH)
            await conn.execute(
                "UPDATE memory_units SET created_at=now()-interval '1 day' WHERE bank_id=$1 AND id=$2", bank, bad
            )
        calls = []

        def response(messages, scope):
            assert scope == "consolidation"
            prompt = str(messages)
            ids = [str(f) for f in (bad, good) if str(f) in prompt]
            calls.append(ids)
            return c._ConsolidationBatchResponse(
                creates=[c._CreateAction(text=SPANISH if str(bad) in ids else ENGLISH, source_fact_ids=ids)]
            )

        with (
            patch.object(memory, "_consolidation_llm_config", _llm(response)),
            patch.object(memory, "submit_async_consolidation"),
            patch.object(
                c, "_find_related_observations", AsyncMock(return_value=SimpleNamespace(results=[], source_facts={}))
            ),
            _override_config(
                memory,
                enable_observations=True,
                llm_language_integrity="reject",
                llm_output_language=None,
                consolidation_dedup_threshold=1.0,
                consolidation_batch_size=batch_size,
                consolidation_llm_batch_size=batch_size,
                consolidation_llm_parallelism=1,
            ),
        ):
            result = await c.run_consolidation_job(memory_engine=memory, bank_id=bank, request_context=request_context)
            await c.run_consolidation_job(memory_engine=memory, bank_id=bank, request_context=request_context)
        prefix = [[str(bad), str(good)], [str(bad), str(good)]] if batch_size == 2 else []
        assert calls == prefix + [[str(bad)], [str(bad)], [str(good)]]
        assert result["memories_failed"] == 1
        assert await _pending_facts(memory, bank) == []
        assert await _observations(memory, bank) == [ENGLISH]
        async with memory._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT text, consolidation_failed_at, consolidated_at FROM memory_units WHERE bank_id=$1 AND id=$2",
                bank,
                bad,
            )
            assert row["text"] == ENGLISH
            assert row["consolidation_failed_at"] is not None
            assert row["consolidated_at"] is None
            assert await conn.fetchval("SELECT count(*) FROM documents WHERE bank_id=$1", bank) == 2
            assert await conn.fetchval("SELECT count(*) FROM chunks WHERE bank_id=$1", bank) == 2
    finally:
        await memory.delete_bank(bank, request_context=request_context)
