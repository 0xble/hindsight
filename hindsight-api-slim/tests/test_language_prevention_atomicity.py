"""Real isolated PostgreSQL, stubbed LLM: rejection preserves sources and prior observations."""

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from hindsight_api.engine.chunk_ids import build_chunk_id
from hindsight_api.engine.consolidation.consolidator import (
    _ConsolidationBatchResponse,
    _CreateAction,
    _DeleteAction,
    run_consolidation_job,
)
from hindsight_api.engine.language_integrity import GeneratedLanguageMismatch
from hindsight_api.engine.response_models import MemoryFact
from tests.test_consolidation_batch_atomicity import (
    _insert_memory,
    _llm,
    _observations,
    _override_config,
    _pending_facts,
)
from tests.test_language_prevention import ENGLISH, SPANISH


@pytest.mark.asyncio
@pytest.mark.memory_backend_incompatible
@pytest.mark.parametrize("corrected", [False, True])
async def test_original_source_rejection_preserves_document_fact_and_observation(memory, request_context, corrected):
    bank = "language-atomic-" + uuid.uuid4().hex[:8]
    await memory.get_bank_profile(bank, request_context=request_context)
    chunk = build_chunk_id(bank, "document", 0)
    old_obs = uuid.uuid4()
    try:
        async with memory._pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO documents (id, bank_id, original_text, content_hash) VALUES ('document', $1, $2, 'hash')",
                bank,
                ENGLISH,
            )
            await conn.execute(
                "INSERT INTO chunks (chunk_id, document_id, bank_id, chunk_index, chunk_text) VALUES ($1, 'document', $2, 0, $3)",
                chunk,
                bank,
                ENGLISH,
            )
            fact = await _insert_memory(conn, bank, SPANISH, [])
            await conn.execute(
                "UPDATE memory_units SET document_id='document', chunk_id=$1 WHERE id=$2 AND bank_id=$3",
                chunk,
                fact,
                bank,
            )
            await conn.execute(
                "INSERT INTO memory_units (id, bank_id, text, fact_type) VALUES ($1, $2, $3, 'observation')",
                old_obs,
                bank,
                "Earlier review awaiting completion.",
            )
        calls = []

        def response(messages, scope):
            assert scope == "consolidation"
            calls.append(messages)
            return _ConsolidationBatchResponse(
                creates=[
                    _CreateAction(
                        text=ENGLISH if corrected and len(calls) == 2 else SPANISH, source_fact_ids=[str(fact)]
                    )
                ],
                deletes=[_DeleteAction(observation_id=str(old_obs))],
            )

        with (
            patch.object(memory, "_consolidation_llm_config", _llm(response)),
            patch.object(memory, "submit_async_consolidation"),
            patch(
                "hindsight_api.engine.consolidation.consolidator._find_related_observations",
                new=AsyncMock(
                    return_value=SimpleNamespace(
                        results=[
                            MemoryFact(
                                id=str(old_obs),
                                text="Earlier review awaiting completion.",
                                fact_type="observation",
                                source_fact_ids=[],
                            )
                        ],
                        source_facts={},
                    )
                ),
            ),
            _override_config(
                memory,
                enable_observations=True,
                llm_language_integrity="reject",
                llm_output_language=None,
                consolidation_llm_batch_size=4,
                consolidation_llm_parallelism=1,
            ),
        ):
            if corrected:
                result = await run_consolidation_job(
                    memory_engine=memory, bank_id=bank, request_context=request_context
                )
                assert result["status"] == "completed"
            else:
                result = await run_consolidation_job(memory_engine=memory, bank_id=bank, request_context=request_context)
                assert result["memories_failed"] == 1
        assert len(calls) == 2
        assert await _observations(memory, bank) == (
            [ENGLISH] if corrected else ["Earlier review awaiting completion."]
        )
        assert await _pending_facts(memory, bank) == []
        async with memory._pool.acquire() as conn:
            assert (
                await conn.fetchval("SELECT original_text FROM documents WHERE id='document' AND bank_id=$1", bank)
                == ENGLISH
            )
            assert (
                await conn.fetchval("SELECT chunk_text FROM chunks WHERE chunk_id=$1 AND bank_id=$2", chunk, bank)
                == ENGLISH
            )
            assert (
                await conn.fetchval("SELECT text FROM memory_units WHERE id=$1 AND bank_id=$2", fact, bank) == SPANISH
            )
            assert await conn.fetchval(
                "SELECT EXISTS(SELECT 1 FROM memory_units WHERE id=$1 AND bank_id=$2)", old_obs, bank
            ) is (not corrected)
            assert bool(await conn.fetchval(
                "SELECT consolidation_failed_at FROM memory_units WHERE id=$1 AND bank_id=$2", fact, bank
            )) is (not corrected)
    finally:
        await memory.delete_bank(bank, request_context=request_context)


@pytest.mark.asyncio
@pytest.mark.memory_backend_incompatible
async def test_retain_rejection_does_not_replace_stored_source(memory, request_context, monkeypatch):
    from tests.test_language_integrity_retain import _llm as extraction_llm

    bank = "language-retain-" + uuid.uuid4().hex[:8]
    await memory.get_bank_profile(bank, request_context=request_context)
    try:
        with _override_config(memory, enable_observations=False, llm_language_integrity="off"):
            await memory.retain_async(
                bank_id=bank, content=ENGLISH, document_id="document", request_context=request_context
            )
        async with memory._pool.acquire() as conn:
            before = await conn.fetch("SELECT id, text FROM memory_units WHERE bank_id=$1 ORDER BY id", bank)
            document_before = await conn.fetchrow(
                "SELECT original_text, content_hash FROM documents WHERE bank_id=$1 AND id='document'", bank
            )
        llm = extraction_llm(SPANISH, SPANISH)
        llm.with_config.return_value = llm
        monkeypatch.setattr(memory, "_retain_llm_config", llm)
        replacement = ENGLISH + " The reviewers added another completed test to the report."
        with _override_config(
            memory, enable_observations=False, llm_language_integrity="reject", llm_output_language=None
        ):
            with pytest.raises(GeneratedLanguageMismatch):
                await memory.retain_async(
                    bank_id=bank, content=replacement, document_id="document", request_context=request_context
                )
        assert llm.call.call_count == 2
        async with memory._pool.acquire() as conn:
            assert await conn.fetch("SELECT id, text FROM memory_units WHERE bank_id=$1 ORDER BY id", bank) == before
            assert (
                await conn.fetchrow(
                    "SELECT original_text, content_hash FROM documents WHERE bank_id=$1 AND id='document'", bank
                )
                == document_before
            )
    finally:
        await memory.delete_bank(bank, request_context=request_context)
