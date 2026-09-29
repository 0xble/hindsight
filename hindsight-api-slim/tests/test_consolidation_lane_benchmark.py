"""Synthetic single-lane consolidation throughput probe (run with pytest -s)."""

import asyncio
import logging
import time
import uuid
from unittest.mock import MagicMock, patch

import pytest

from hindsight_api.engine.consolidation.consolidator import (
    _ConsolidationBatchResponse,
    _CreateAction,
    _UpdateAction,
    run_consolidation_job,
)
from hindsight_api.engine.memory_engine import MemoryEngine
from hindsight_api.engine.providers.mock_llm import MockLLM
from hindsight_api.engine.response_models import MemoryFact, RecallResult
from tests.test_consolidation_scope_parallelism import _insert_memory, _override_config


@pytest.mark.asyncio
@pytest.mark.memory_backend_incompatible
async def test_single_lane_benchmark(memory: MemoryEngine, request_context, caplog):
    from hindsight_api.engine.consolidation import consolidator as mod

    for lane_parallelism in (1, 8):
        bank_id = f"test-lane-bench-{uuid.uuid4().hex[:8]}"
        await memory.ensure_bank_profile(bank_id=bank_id, request_context=request_context)
        observation_id = uuid.uuid4()
        facts = []
        try:
            async with memory._pool.acquire() as conn:
                for index in range(24):
                    facts.append(await _insert_memory(conn, bank_id, f"Shared topic {index}", [], "shared"))
                await conn.execute(
                    "INSERT INTO memory_units (id, bank_id, text, fact_type, tags, source_memory_ids, created_at) "
                    "VALUES ($1,$2,'Original topic','observation','{}',$3,now())",
                    observation_id,
                    bank_id,
                    [str(facts[0])],
                )

            async def find(*, memory_engine, bank_id, query, request_context, tags=None):
                async with memory._pool.acquire() as conn:
                    row = await conn.fetchrow(
                        "SELECT text, source_memory_ids FROM memory_units WHERE id=$1", observation_id
                    )
                # Half the facts overlap on the observation; the rest create distinct observations.
                index = int(query.rsplit(" ", 1)[1])
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
                    if index < 12
                    else []
                )

            mock_llm = MockLLM(provider="mock", api_key="", base_url="", model="mock-model")

            def response(messages, scope):
                if scope != "consolidation":
                    return _ConsolidationBatchResponse()
                prompt = "\n".join(m.get("content", "") for m in messages if m.get("role") == "user")
                fact_id = next(str(fact_id) for fact_id in facts if str(fact_id) in prompt)
                index = facts.index(uuid.UUID(fact_id))
                if index < 12:
                    return _ConsolidationBatchResponse(
                        updates=[
                            _UpdateAction(
                                observation_id=str(observation_id),
                                text=f"Updated topic {index}",
                                source_fact_ids=[fact_id],
                            )
                        ]
                    )
                return _ConsolidationBatchResponse(
                    creates=[_CreateAction(text=f"Unique observation {index}", source_fact_ids=[fact_id])]
                )

            mock_llm.set_response_callback(response)
            wrapper = MagicMock()
            wrapper.with_config.return_value = mock_llm
            original_llm_config = memory._consolidation_llm_config
            original_llm = mod._consolidate_batch_with_llm
            memory._consolidation_llm_config = wrapper

            async def delayed(*args, **kwargs):
                await asyncio.sleep(0.05)
                return await original_llm(*args, **kwargs)

            try:
                with (
                    _override_config(
                        memory,
                        consolidation_llm_parallelism=8,
                        consolidation_lane_llm_parallelism=lane_parallelism,
                        consolidation_llm_batch_size=1,
                        consolidation_dedup_threshold=1.0,
                    ),
                    patch.object(memory, "submit_async_consolidation"),
                    patch.object(mod, "_find_related_observations", find),
                    patch.object(mod, "_consolidate_batch_with_llm", delayed),
                    caplog.at_level(logging.WARNING, logger=mod.__name__),
                ):
                    start = time.perf_counter()
                    result = await run_consolidation_job(
                        memory_engine=memory, bank_id=bank_id, request_context=request_context
                    )
                    elapsed = time.perf_counter() - start
            finally:
                memory._consolidation_llm_config = original_llm_config
            async with memory._pool.acquire() as conn:
                states = await conn.fetch(
                    "SELECT consolidated_at, consolidation_failed_at FROM memory_units WHERE id=ANY($1::uuid[])", facts
                )
                duplicates = await conn.fetchval(
                    "SELECT count(*) FROM (SELECT text FROM memory_units WHERE bank_id=$1 "
                    "AND fact_type='observation' GROUP BY text HAVING count(*)>1) duplicate_texts",
                    bank_id,
                )
            retries = sum("stale prepared reference" in rec.message for rec in caplog.records)
            print(
                f"BENCH lane={lane_parallelism} facts={len(facts)} elapsed={elapsed:.3f}s "
                f"facts_per_sec={len(facts) / elapsed:.2f} retries={retries} duplicates={duplicates} "
                f"consolidated={sum(s['consolidated_at'] is not None for s in states)} "
                f"failed={sum(s['consolidation_failed_at'] is not None for s in states)}"
            )
            assert result["status"] == "completed"
            assert all(s["consolidated_at"] is not None for s in states)
            assert duplicates == 0
            caplog.clear()
        finally:
            await memory.delete_bank(bank_id, request_context=request_context)
