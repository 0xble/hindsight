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
    _UpdateAction,
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
    await memory.ensure_bank_profile(bank, request_context=request_context)
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
                result = await run_consolidation_job(
                    memory_engine=memory, bank_id=bank, request_context=request_context
                )
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
            assert bool(
                await conn.fetchval(
                    "SELECT consolidation_failed_at FROM memory_units WHERE id=$1 AND bank_id=$2", fact, bank
                )
            ) is (not corrected)
    finally:
        await memory.delete_bank(bank, request_context=request_context)


@pytest.mark.asyncio
@pytest.mark.memory_backend_incompatible
async def test_cross_recall_update_response_rejects_whole_real_pg_batch_without_writes_or_stamps(
    memory, request_context
):
    """A union-visible target is not writable through an unrelated cited fact."""
    bank = "language-topology-" + uuid.uuid4().hex[:8]
    await memory.ensure_bank_profile(bank, request_context=request_context)
    target = uuid.uuid4()
    try:
        async with memory._pool.acquire() as conn:
            fact_a = await _insert_memory(conn, bank, "Fact A is only related to its own context.", [])
            fact_b = await _insert_memory(conn, bank, "Fact B is related to observation O.", [])
            await conn.execute(
                "INSERT INTO memory_units (id, bank_id, text, fact_type) VALUES ($1, $2, $3, 'observation')",
                target,
                bank,
                "Observation O must survive the rejected response.",
            )

        def response(_messages, scope):
            assert scope == "consolidation"
            return _ConsolidationBatchResponse(
                creates=[_CreateAction(text="A valid-looking sibling create.", source_fact_ids=[str(fact_b)])],
                updates=[
                    _UpdateAction(
                        observation_id=str(target),
                        text="This update cites A but O was recalled only for B.",
                        source_fact_ids=[str(fact_a)],
                    )
                ],
                deletes=[_DeleteAction(observation_id=str(target))],
            )

        async def recalled(*, query, **_kwargs):
            if query.startswith("Fact B"):
                return SimpleNamespace(
                    results=[
                        MemoryFact(
                            id=str(target),
                            text="Observation O must survive the rejected response.",
                            fact_type="observation",
                            source_fact_ids=[],
                        )
                    ],
                    source_facts={},
                )
            return SimpleNamespace(results=[], source_facts={})

        with (
            patch.object(memory, "_consolidation_llm_config", _llm(response)),
            patch.object(memory, "submit_async_consolidation"),
            patch(
                "hindsight_api.engine.consolidation.consolidator._find_related_observations",
                new=AsyncMock(side_effect=recalled),
            ),
            _override_config(
                memory,
                enable_observations=True,
                llm_language_integrity="off",
                consolidation_batch_size=2,
                consolidation_llm_batch_size=2,
                consolidation_llm_parallelism=1,
            ),
        ):
            result = await run_consolidation_job(memory_engine=memory, bank_id=bank, request_context=request_context)

        assert result["memories_failed"] == 2
        assert await _observations(memory, bank) == ["Observation O must survive the rejected response."]
        # The existing failed-fact lifecycle holds the facts for recovery, but
        # no response-derived success stamp or write can escape the preflight.
        assert await _pending_facts(memory, bank) == []
        async with memory._pool.acquire() as conn:
            stamps = await conn.fetch(
                "SELECT consolidated_at, consolidation_failed_at FROM memory_units WHERE bank_id=$1 AND id = ANY($2)",
                bank,
                [fact_a, fact_b],
            )
        assert all(row["consolidated_at"] is None and row["consolidation_failed_at"] is not None for row in stamps)
    finally:
        await memory.delete_bank(bank, request_context=request_context)


@pytest.mark.asyncio
@pytest.mark.memory_backend_incompatible
async def test_invalid_citation_drops_only_its_action_and_valid_sibling_commits_real_pg(memory, request_context):
    """Without deletes, an action citing a non-batch fact is dropped and its sibling writes and stamps."""
    bank = "language-drop-" + uuid.uuid4().hex[:8]
    await memory.ensure_bank_profile(bank, request_context=request_context)
    try:
        async with memory._pool.acquire() as conn:
            fact_a = await _insert_memory(conn, bank, "Fact A is about the garden.", [])
            fact_b = await _insert_memory(conn, bank, "Fact B is about the kitchen.", [])
        outsider = str(uuid.uuid4())

        def response(_messages, scope):
            assert scope == "consolidation"
            return _ConsolidationBatchResponse(
                creates=[
                    _CreateAction(
                        text="The garden and kitchen were both renovated.", source_fact_ids=[str(fact_a), str(fact_b)]
                    ),
                    _CreateAction(
                        text="Cites a fact that is not in this batch.", source_fact_ids=[str(fact_a), outsider]
                    ),
                ],
            )

        with (
            patch.object(memory, "_consolidation_llm_config", _llm(response)),
            patch.object(memory, "submit_async_consolidation"),
            patch(
                "hindsight_api.engine.consolidation.consolidator._find_related_observations",
                new=AsyncMock(return_value=SimpleNamespace(results=[], source_facts={})),
            ),
            _override_config(
                memory,
                enable_observations=True,
                llm_language_integrity="off",
                consolidation_batch_size=2,
                consolidation_llm_batch_size=2,
                consolidation_llm_parallelism=1,
            ),
        ):
            result = await run_consolidation_job(memory_engine=memory, bank_id=bank, request_context=request_context)

        assert result["memories_failed"] == 0
        assert await _observations(memory, bank) == ["The garden and kitchen were both renovated."]
        assert await _pending_facts(memory, bank) == []
        async with memory._pool.acquire() as conn:
            stamps = await conn.fetch(
                "SELECT consolidated_at, consolidation_failed_at FROM memory_units WHERE bank_id=$1 AND id = ANY($2)",
                bank,
                [fact_a, fact_b],
            )
        assert all(row["consolidated_at"] is not None and row["consolidation_failed_at"] is None for row in stamps)
    finally:
        await memory.delete_bank(bank, request_context=request_context)


@pytest.mark.asyncio
@pytest.mark.memory_backend_incompatible
async def test_partial_invalid_citation_commits_covered_fact_and_retries_only_pending_real_pg(memory, request_context):
    bank = "language-partial-" + uuid.uuid4().hex[:8]
    await memory.ensure_bank_profile(bank, request_context=request_context)
    try:
        async with memory._pool.acquire() as conn:
            fact_a = await _insert_memory(conn, bank, "Garden was renovated.", [])
            fact_b = await _insert_memory(conn, bank, "Kitchen was painted.", [])
        outsider = str(uuid.uuid4())
        calls = []

        def response(messages, scope):
            assert scope == "consolidation"
            calls.append(messages)
            if len(calls) == 1:
                return _ConsolidationBatchResponse(
                    creates=[
                        _CreateAction(text="Garden was renovated.", source_fact_ids=[str(fact_a)]),
                        _CreateAction(text="Invalid kitchen citation.", source_fact_ids=[str(fact_b), outsider]),
                    ]
                )
            return _ConsolidationBatchResponse(
                creates=[
                    _CreateAction(text="Kitchen was painted.", source_fact_ids=[str(fact_b)]),
                ]
            )

        with (
            patch.object(memory, "_consolidation_llm_config", _llm(response)),
            patch(
                "hindsight_api.engine.consolidation.consolidator._find_related_observations",
                new=AsyncMock(return_value=SimpleNamespace(results=[], source_facts={})),
            ),
            _override_config(
                memory,
                enable_observations=True,
                llm_language_integrity="off",
                consolidation_batch_size=2,
                consolidation_llm_batch_size=2,
                consolidation_llm_parallelism=1,
            ),
        ):
            first = await run_consolidation_job(memory_engine=memory, bank_id=bank, request_context=request_context)
            assert first["memories_failed"] == 0
            assert len(calls) == 1  # No same-job retry loop or bisection.
            assert await _observations(memory, bank) == ["Garden was renovated."]
            assert await _pending_facts(memory, bank) == ["Kitchen was painted."]
            units = (await memory.list_memory_units(bank, request_context=request_context))["items"]
            by_id = {unit["id"]: unit for unit in units}
            assert by_id[str(fact_a)]["consolidated_at"] is not None
            assert by_id[str(fact_a)]["consolidation_failed_at"] is None
            assert by_id[str(fact_b)]["consolidated_at"] is None
            assert by_id[str(fact_b)]["consolidation_failed_at"] is None
            garden = next(
                unit for unit in units if unit["text"] == "Garden was renovated." and unit["fact_type"] == "observation"
            )
            assert garden["source_memory_ids"] == [str(fact_a)]
            second = await run_consolidation_job(memory_engine=memory, bank_id=bank, request_context=request_context)
            assert second["memories_failed"] == 0
            assert len(calls) == 2
            assert await _pending_facts(memory, bank) == []
            assert sorted(await _observations(memory, bank)) == ["Garden was renovated.", "Kitchen was painted."]
    finally:
        await memory.delete_bank(bank, request_context=request_context)


@pytest.mark.asyncio
@pytest.mark.memory_backend_incompatible
async def test_persistently_miscited_single_fact_is_failed_real_pg(memory, request_context):
    bank = "language-leaf-" + uuid.uuid4().hex[:8]
    await memory.ensure_bank_profile(bank, request_context=request_context)
    try:
        async with memory._pool.acquire() as conn:
            fact = await _insert_memory(conn, bank, "Persistently mis-cited fact.", [])
        calls = []

        def response(_messages, _scope):
            calls.append(1)
            return _ConsolidationBatchResponse(
                creates=[_CreateAction(text="Invalid.", source_fact_ids=[str(fact), str(uuid.uuid4())])]
            )

        with (
            patch.object(memory, "_consolidation_llm_config", _llm(response)),
            patch(
                "hindsight_api.engine.consolidation.consolidator._find_related_observations",
                new=AsyncMock(return_value=SimpleNamespace(results=[], source_facts={})),
            ),
            _override_config(
                memory,
                enable_observations=True,
                llm_language_integrity="off",
                consolidation_batch_size=1,
                consolidation_llm_batch_size=1,
            ),
        ):
            result = await run_consolidation_job(memory_engine=memory, bank_id=bank, request_context=request_context)
            assert result["memories_failed"] == 1
            assert result["memories_processed"] == 1
            assert result["memories_deferred"] == 0
            assert len(calls) == 1
            again = await run_consolidation_job(memory_engine=memory, bank_id=bank, request_context=request_context)
            assert again["memories_processed"] == 0
        assert await _pending_facts(memory, bank) == []
        async with memory._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT consolidated_at, consolidation_failed_at FROM memory_units WHERE bank_id=$1 AND id=$2",
                bank,
                fact,
            )
        assert row["consolidated_at"] is None and row["consolidation_failed_at"] is not None
    finally:
        await memory.delete_bank(bank, request_context=request_context)


@pytest.mark.asyncio
@pytest.mark.memory_backend_incompatible
async def test_deferred_fact_does_not_block_later_fetch_or_retry_in_same_job_real_pg(memory, request_context):
    bank = "language-backlog-" + uuid.uuid4().hex[:8]
    await memory.ensure_bank_profile(bank, request_context=request_context)
    try:
        async with memory._pool.acquire() as conn:
            fact_a = await _insert_memory(conn, bank, "Garden was renovated.", [])
            fact_b = await _insert_memory(conn, bank, "Kitchen was painted.", [])
            fact_c = await _insert_memory(conn, bank, "Patio was cleaned.", [])
        calls = []
        outsider = str(uuid.uuid4())

        def response(messages, _scope):
            calls.append(messages)
            if len(calls) == 1:
                return _ConsolidationBatchResponse(
                    creates=[
                        _CreateAction(text="Garden was renovated.", source_fact_ids=[str(fact_a)]),
                        _CreateAction(text="Invalid kitchen.", source_fact_ids=[str(fact_b), outsider]),
                    ]
                )
            if len(calls) == 2:
                return _ConsolidationBatchResponse(
                    creates=[_CreateAction(text="Patio was cleaned.", source_fact_ids=[str(fact_c)])]
                )
            return _ConsolidationBatchResponse(
                creates=[_CreateAction(text="Kitchen was painted.", source_fact_ids=[str(fact_b)])]
            )

        with (
            patch.object(memory, "_consolidation_llm_config", _llm(response)),
            patch(
                "hindsight_api.engine.consolidation.consolidator._find_related_observations",
                new=AsyncMock(return_value=SimpleNamespace(results=[], source_facts={})),
            ),
            _override_config(
                memory,
                enable_observations=True,
                llm_language_integrity="off",
                consolidation_batch_size=2,
                consolidation_llm_batch_size=2,
                consolidation_llm_parallelism=1,
            ),
        ):
            first = await run_consolidation_job(memory_engine=memory, bank_id=bank, request_context=request_context)
            assert len(calls) == 2  # B was excluded from the second fetch, not retried.
            assert first["memories_processed"] == 2
            assert first["memories_deferred"] == 1
            assert first["skipped"] == 0
            assert await _pending_facts(memory, bank) == ["Kitchen was painted."]
            assert sorted(await _observations(memory, bank)) == ["Garden was renovated.", "Patio was cleaned."]
            second = await run_consolidation_job(memory_engine=memory, bank_id=bank, request_context=request_context)
            assert second["memories_processed"] == 1
            assert len(calls) == 3
            assert await _pending_facts(memory, bank) == []
    finally:
        await memory.delete_bank(bank, request_context=request_context)


@pytest.mark.asyncio
@pytest.mark.memory_backend_incompatible
async def test_deferred_fact_round_limit_requeues_with_real_remaining_real_pg(memory, request_context):
    bank = "language-requeue-" + uuid.uuid4().hex[:8]
    await memory.ensure_bank_profile(bank, request_context=request_context)
    try:
        async with memory._pool.acquire() as conn:
            fact_a = await _insert_memory(conn, bank, "Garden was renovated.", [])
            fact_b = await _insert_memory(conn, bank, "Kitchen was painted.", [])
            await _insert_memory(conn, bank, "Patio was cleaned.", [])
        outsider = str(uuid.uuid4())

        def response(_messages, _scope):
            return _ConsolidationBatchResponse(
                creates=[
                    _CreateAction(text="Garden was renovated.", source_fact_ids=[str(fact_a)]),
                    _CreateAction(text="Invalid kitchen.", source_fact_ids=[str(fact_b), outsider]),
                ]
            )

        with (
            patch.object(memory, "_consolidation_llm_config", _llm(response)),
            patch.object(memory, "submit_async_consolidation", new_callable=AsyncMock) as requeue,
            patch(
                "hindsight_api.engine.consolidation.consolidator._find_related_observations",
                new=AsyncMock(return_value=SimpleNamespace(results=[], source_facts={})),
            ),
            _override_config(
                memory,
                enable_observations=True,
                llm_language_integrity="off",
                consolidation_batch_size=2,
                consolidation_llm_batch_size=2,
                consolidation_llm_parallelism=1,
                consolidation_max_memories_per_round=2,
            ),
        ):
            result = await run_consolidation_job(memory_engine=memory, bank_id=bank, request_context=request_context)
        assert result["memories_processed"] == 1
        assert result["memories_deferred"] == 1
        assert await _pending_facts(memory, bank) == ["Kitchen was painted.", "Patio was cleaned."]
        requeue.assert_awaited_once()
    finally:
        await memory.delete_bank(bank, request_context=request_context)


@pytest.mark.asyncio
@pytest.mark.memory_backend_incompatible
async def test_earlier_scope_pending_stays_unstamped_after_later_scope_write_real_pg(memory, request_context):
    bank = "language-scopes-" + uuid.uuid4().hex[:8]
    await memory.ensure_bank_profile(bank, request_context=request_context)
    try:
        async with memory._pool.acquire() as conn:
            fact_a = await _insert_memory(conn, bank, "Garden was renovated.", ["garden", "shared"])
            fact_b = await _insert_memory(conn, bank, "Kitchen was painted.", ["garden", "shared"])
            await conn.execute(
                "UPDATE memory_units SET observation_scopes=$1::jsonb WHERE id = ANY($2)",
                '"per_tag"',
                [fact_a, fact_b],
            )
        calls = []
        outsider = str(uuid.uuid4())

        def response(_messages, _scope):
            calls.append(1)
            if len(calls) == 1:
                return _ConsolidationBatchResponse(
                    creates=[
                        _CreateAction(text="Garden was renovated.", source_fact_ids=[str(fact_a)]),
                        _CreateAction(text="Invalid kitchen.", source_fact_ids=[str(fact_b), outsider]),
                    ]
                )
            return _ConsolidationBatchResponse(
                creates=[
                    _CreateAction(text="Garden was renovated.", source_fact_ids=[str(fact_a)]),
                    _CreateAction(text="Kitchen was painted.", source_fact_ids=[str(fact_b)]),
                ]
            )

        with (
            patch.object(memory, "_consolidation_llm_config", _llm(response)),
            patch(
                "hindsight_api.engine.consolidation.consolidator._find_related_observations",
                new=AsyncMock(return_value=SimpleNamespace(results=[], source_facts={})),
            ),
            _override_config(
                memory,
                enable_observations=True,
                llm_language_integrity="off",
                consolidation_batch_size=2,
                consolidation_llm_batch_size=2,
                consolidation_llm_parallelism=1,
            ),
        ):
            result = await run_consolidation_job(memory_engine=memory, bank_id=bank, request_context=request_context)
        assert len(calls) == 2
        assert result["memories_processed"] == 1
        assert result["memories_deferred"] == 1
        assert result["memories_failed"] == 0
        assert await _pending_facts(memory, bank) == ["Kitchen was painted."]
        async with memory._pool.acquire() as conn:
            stamps = await conn.fetch(
                "SELECT id, consolidated_at FROM memory_units WHERE bank_id=$1 AND id = ANY($2)",
                bank,
                [fact_a, fact_b],
            )
        by_id = {row["id"]: row["consolidated_at"] for row in stamps}
        assert by_id[fact_a] is not None and by_id[fact_b] is None
    finally:
        await memory.delete_bank(bank, request_context=request_context)


@pytest.mark.asyncio
@pytest.mark.memory_backend_incompatible
async def test_filtered_reply_stamps_valid_duplicate_create_real_pg(memory, request_context):
    bank = "language-no-coverage-" + uuid.uuid4().hex[:8]
    await memory.ensure_bank_profile(bank, request_context=request_context)
    try:
        async with memory._pool.acquire() as conn:
            fact_a = await _insert_memory(conn, bank, "Already known.", [])
            fact_b = await _insert_memory(conn, bank, "Another fact.", [])
            prior = uuid.uuid4()
            await conn.execute(
                "INSERT INTO memory_units (id, bank_id, text, fact_type) VALUES ($1, $2, $3, 'observation')",
                prior,
                bank,
                "Already known.",
            )

        def response(_messages, _scope):
            return _ConsolidationBatchResponse(
                creates=[
                    _CreateAction(text="Already known.", source_fact_ids=[str(fact_a)]),
                    _CreateAction(text="Invalid.", source_fact_ids=[str(fact_b), str(uuid.uuid4())]),
                ]
            )

        with (
            patch.object(memory, "_consolidation_llm_config", _llm(response)),
            patch(
                "hindsight_api.engine.consolidation.consolidator._find_related_observations",
                new=AsyncMock(
                    return_value=SimpleNamespace(
                        results=[
                            MemoryFact(
                                id=str(prior), text="Already known.", fact_type="observation", source_fact_ids=[]
                            )
                        ],
                        source_facts={},
                    )
                ),
            ),
            _override_config(
                memory,
                enable_observations=True,
                llm_language_integrity="off",
                consolidation_batch_size=2,
                consolidation_llm_batch_size=2,
                consolidation_llm_parallelism=1,
            ),
        ):
            result = await run_consolidation_job(memory_engine=memory, bank_id=bank, request_context=request_context)
        assert result["memories_failed"] == 0
        assert result["memories_processed"] == 1
        assert result["memories_deferred"] == 1
        assert await _pending_facts(memory, bank) == ["Another fact."]
        assert await _observations(memory, bank) == ["Already known."]
        units = (await memory.list_memory_units(bank, request_context=request_context))["items"]
        by_id = {unit["id"]: unit for unit in units}
        assert by_id[str(fact_a)]["consolidated_at"] is not None
        assert by_id[str(fact_b)]["consolidated_at"] is None
    finally:
        await memory.delete_bank(bank, request_context=request_context)


@pytest.mark.asyncio
@pytest.mark.memory_backend_incompatible
async def test_retain_rejection_does_not_replace_stored_source(memory, request_context, monkeypatch):
    from tests.test_language_integrity_retain import _llm as extraction_llm

    bank = "language-retain-" + uuid.uuid4().hex[:8]
    await memory.ensure_bank_profile(bank, request_context=request_context)
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
