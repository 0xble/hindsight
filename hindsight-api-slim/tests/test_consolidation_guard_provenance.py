"""Detail fallbacks retain their own citations beside native exact-fold targets."""

import asyncio
import uuid
from dataclasses import replace
from unittest.mock import patch

import asyncpg
import pytest

from hindsight_api.config import _get_raw_config
from hindsight_api.engine.consolidation import consolidator as c
from hindsight_api.engine.response_models import MemoryFact, RecallResult
from tests.test_consolidation_schema_correction import install, provider  # noqa: F401
from tests.test_consolidation_scope_parallelism import _insert_memory


@pytest.mark.asyncio
@pytest.mark.memory_backend_incompatible
@pytest.mark.parametrize(
    "variant,lane",
    [(variant, lane) for variant in ("same-text", "at-cap", "rollback") for lane in (False, True)]
    # The job owner disables parallel lane apply for stores and Oracle.
    + [("store", False), ("oracle", False)],
)
async def test_guard_fallback_and_exact_twin_keep_distinct_source_coverage(
    memory, request_context, provider, lane, variant
) -> None:
    bank = "guard-provenance-" + uuid.uuid4().hex[:8]
    target, twin = uuid.uuid4(), uuid.uuid4()
    before, after = "Server timeout is 5 seconds.", "Server is monitored."
    tags = ["guarded"]
    await memory.ensure_bank_profile(bank, request_context=request_context)
    try:
        async with memory._pool.acquire() as conn:
            old = await _insert_memory(conn, bank, before, tags, "shared")
            fresh = await _insert_memory(conn, bank, after, tags, "shared")
            extra = await _insert_memory(conn, bank, "Independent monitoring source.", tags, "shared")
            twin_source = await _insert_memory(conn, bank, after, tags, "shared")
            for oid, text, source in [(target, before, old), (twin, after, twin_source)]:
                await conn.execute(
                    "INSERT INTO memory_units (id,bank_id,text,fact_type,tags,source_memory_ids) "
                    "VALUES ($1,$2,$3,'observation',$4,$5)",
                    oid,
                    bank,
                    text,
                    tags,
                    [source],
                )
            facts = [dict(await conn.fetchrow("SELECT * FROM memory_units WHERE id=$1", fid)) for fid in (fresh, extra)]
        observations = [
            MemoryFact.model_construct(
                id=str(oid), text=text, fact_type="observation", tags=tags, source_fact_ids=[str(source)]
            )
            for oid, text, source in [(target, before, old), (twin, after, twin_source)]
        ]
        payload = {
            "updates": [{"observation_id": str(target), "text": after, "source_fact_ids": [str(fresh)]}],
            "creates": [{"text": after, "source_fact_ids": [str(extra)]}],
            "deletes": [{"observation_id": str(target)}],
        }
        if variant == "at-cap":
            # A provider obeys the zero-CREATE response schema. Only the guard's
            # internal fallback can insert beyond the soft observation cap.
            payload["creates"] = []
        stub = install(provider, [payload])
        store = c.get_memories()
        if variant == "store":
            from tests.test_memories_extension import InMemoryMemories, _stored

            store = InMemoryMemories({})
            for fid, text in [
                (old, before),
                (fresh, after),
                (extra, "Independent monitoring source."),
                (twin_source, after),
            ]:
                store.rows[str(fid)] = _stored(str(fid), text, "experience", tags=tags)
            for oid, text, source in [(target, before, old), (twin, after, twin_source)]:
                store.rows[str(oid)] = _stored(
                    str(oid), text, "observation", tags=tags, source_memory_ids=[str(source)]
                )
        config = replace(
            _get_raw_config(),
            consolidation_dedup_threshold=1.0,
            llm_language_integrity="off",
            max_observations_per_scope=2 if variant == "at-cap" else -1,
        )
        original_stamp = store.mark_consolidated
        attempts = 0

        async def stamp(**kwargs):
            nonlocal attempts
            attempts += 1
            if variant == "rollback" and attempts == 1:
                raise asyncpg.DeadlockDetectedError("controlled rollback after guard fallback insert")
            return await original_stamp(**kwargs)

        ready = asyncio.Event()
        ready.set()
        with (
            patch.object(c, "get_memories", return_value=store),
            patch.object(
                c,
                "get_config",
                return_value=replace(_get_raw_config(), database_backend="oracle")
                if variant == "oracle"
                else _get_raw_config(),
            ),
            patch.object(
                c, "_find_related_observations", return_value=RecallResult.model_construct(results=observations)
            ),
            patch.object(store, "mark_consolidated", stamp),
        ):
            _, _, failed = await c._process_memory_batch(
                pool=memory._backend,
                memory_engine=memory,
                llm_config=provider,
                bank_id=bank,
                memories=facts,
                request_context=request_context,
                config=config,
                obs_tags_override=tags,
                mark_consolidated_ids=[fresh, extra],
                schema_correction_budget=c._SchemaCorrectionBudget(0),
                apply_turn=(ready, asyncio.Event()) if lane else None,
            )
        assert not failed
        assert len(stub.requests) == 1
        if variant == "store":
            rows = [row for row in store.rows.values() if row.fact_type == "observation"]
            persisted = {(row.text, frozenset(uuid.UUID(s) for s in row.source_memory_ids)) for row in rows}
            assert store.rows[str(fresh)].consolidated_at is not None
        else:
            async with memory._pool.acquire() as conn:
                rows = await conn.fetch(
                    "SELECT text,source_memory_ids FROM memory_units WHERE bank_id=$1 AND fact_type='observation'", bank
                )
                persisted = {(row["text"], frozenset(row["source_memory_ids"])) for row in rows}
                assert await conn.fetchval("SELECT consolidated_at FROM memory_units WHERE id=$1", fresh) is not None
        expected = {(before, frozenset([old])), (after, frozenset([fresh]))}
        if variant in {"store", "oracle"}:
            expected |= {(after, frozenset([twin_source])), (after, frozenset([extra]))}
        elif variant == "at-cap":
            expected.add((after, frozenset([twin_source])))
        else:
            expected.add((after, frozenset([twin_source, extra])))
        assert persisted == expected
        if variant == "rollback":
            assert attempts == 2
    finally:
        await memory.delete_bank(bank, request_context=request_context)
