"""Real PostgreSQL proofs for the bounded, conditional recovery protocol.

These tests seed exact history/link identities through SQL, then drive public
engine/HTTP boundaries. The system story separately exercises client and worker
composition without SQL or internal imports.
"""

import asyncio
import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, localcontext
from unittest.mock import AsyncMock, patch

import asyncpg
import httpx
import pytest
import pytest_asyncio

from hindsight_api import RequestContext
from hindsight_api.api import create_app
from hindsight_api.engine.cross_encoder import RRFPassthroughCrossEncoder
from hindsight_api.engine.curation_batch import (
    CurationApplyRequest,
    CurationBatchConflict,
    CurationChange,
    CurationFields,
    CurationPreviewRequest,
    CurationRevertRequest,
    TableSnapshot,
    canonical_bytes,
    revision,
    snapshot_revision,
)
from hindsight_api.engine.embeddings import Embeddings
from hindsight_api.engine.memories import get_memories
from hindsight_api.engine.memories.pg import curation_batch as store
from hindsight_api.engine.memories.pg import graph
from hindsight_api.engine.memory_engine import MemoryEngine
from hindsight_api.engine.schema import fq_table
from hindsight_api.engine.task_backend import SyncTaskBackend

pytestmark = [pytest.mark.asyncio, pytest.mark.memory_backend_incompatible]
CTX = RequestContext()


class SyntheticEmbeddings(Embeddings):
    provider_name = "synthetic-curation"
    dimension = 384

    async def initialize(self):
        pass

    async def encode(self, texts):
        return [[0.25] * 384 for _ in texts]


@pytest_asyncio.fixture
async def memory(pg0_db_url):
    mem = MemoryEngine(
        db_url=pg0_db_url,
        memory_llm_provider="mock",
        memory_llm_api_key="",
        memory_llm_model="mock",
        embeddings=SyntheticEmbeddings(),
        cross_encoder=RRFPassthroughCrossEncoder(),
        pool_min_size=1,
        pool_max_size=4,
        run_migrations=False,
        task_backend=SyncTaskBackend(),
    )
    await mem.initialize()
    yield mem
    await mem.close()


@dataclass
class Seed:
    bank: str
    raw: uuid.UUID
    peer: uuid.UUID
    observation: uuid.UUID
    entities: list[uuid.UUID]
    history: list[int]


async def raw(conn, bank, text="Alice works on a historical system", *, doc=None):
    mid = uuid.uuid4()
    doc = doc or f"document-{mid}"
    chunk = f"{bank}_{doc}_0"
    await conn.execute(
        "INSERT INTO documents(id,bank_id,original_text,content_hash) VALUES($1,$2,$3,'fixture')", doc, bank, text
    )
    await conn.execute(
        "INSERT INTO chunks(chunk_id,document_id,bank_id,chunk_index,chunk_text) VALUES($1,$2,$3,0,$4)",
        chunk,
        doc,
        bank,
        text,
    )
    await conn.execute(
        "INSERT INTO memory_units(id,bank_id,text,fact_type,document_id,chunk_id,embedding,event_date,occurred_start,occurred_end,mentioned_at,consolidated_at) "
        "VALUES($1,$2,$3,'world',$4,$5,$6::vector,'2020-01-01','2020-01-01','2020-01-01','2026-10-01',now())",
        mid,
        bank,
        text,
        doc,
        chunk,
        str([0.25] * 384),
    )
    return mid


@pytest_asyncio.fixture
async def seeded(memory):
    bank = f"test-curation-v2-{uuid.uuid4().hex}"
    await memory.ensure_bank_profile(bank, request_context=CTX)
    await memory.update_bank_config(bank, {"enable_auto_consolidation": False}, request_context=CTX)
    async with memory._pool.acquire() as conn:
        raw_id = await raw(conn, bank)
        peer = await raw(conn, bank, "Alice also holds a corroborating fact")
        obs = uuid.uuid4()
        await conn.execute(
            "INSERT INTO memory_units(id,bank_id,text,fact_type,source_memory_ids,event_date,embedding,proof_count) VALUES($1,$2,'Derived historical observation','observation',$3,'2020-01-01',$4::vector,2)",
            obs,
            bank,
            [raw_id, peer],
            str([0.25] * 384),
        )
        entities = sorted([uuid.uuid4(), uuid.uuid4()])
        for index, eid in enumerate(entities):
            await conn.execute(
                "INSERT INTO entities(id,bank_id,canonical_name,mention_count) VALUES($1,$2,$3,2)",
                eid,
                bank,
                f"Alice-{index}",
            )
            await conn.execute("INSERT INTO unit_entities(unit_id,entity_id) VALUES($1,$2),($3,$2)", raw_id, eid, obs)
        await conn.execute(
            "INSERT INTO entity_cooccurrences(entity_id_1,entity_id_2,cooccurrence_count) VALUES($1,$2,2)", *entities
        )
        await conn.execute(
            "INSERT INTO memory_links(bank_id,from_unit_id,to_unit_id,entity_id,link_type,weight) VALUES($1,$2,$3,$4,'entity',0.8)",
            bank,
            raw_id,
            peer,
            entities[0],
        )
        history = []
        for index in range(2):
            history.append(
                await conn.fetchval(
                    "INSERT INTO observation_history(observation_id,bank_id,content,changed_at) VALUES($1,$2,$3::jsonb,'2025-05-05') RETURNING id",
                    obs,
                    bank,
                    json.dumps({"previous_text": f"version {index}", "numeric": 9007199254740993}),
                )
            )
    yield Seed(bank, raw_id, peer, obs, entities, history)
    # Cleanup must bypass the public delete guard only in this synthetic fixture.
    async with memory._pool.acquire() as conn:
        await conn.execute("DELETE FROM curation_batches WHERE bank_id=$1", bank)
    await memory.delete_bank(bank, request_context=CTX)


async def preview(memory, seed, ids=None):
    return await memory.preview_curation_batch(
        seed.bank, CurationPreviewRequest(protocol="raw-curation-v2", memory_ids=ids or [seed.raw]), request_context=CTX
    )


def manifest(view, *, correction=None):
    return CurationApplyRequest(
        protocol="raw-curation-v2",
        expected_closure_revision=view.closure_revision,
        changes=[
            CurationChange(
                **t.model_dump(),
                action="correct" if correction else "invalidate",
                reason="canonical source contradicts extracted fact",
                fields=correction,
            )
            for t in view.targets
        ],
    )


async def apply(memory, seed, request, batch="fixture"):
    return await memory.apply_curation_batch(seed.bank, batch, request, request_context=CTX)


async def revert(memory, seed, receipt, batch="fixture"):
    return await memory.revert_curation_batch(
        seed.bank,
        batch,
        CurationRevertRequest(protocol="raw-curation-v2", expected_receipt_revision=receipt.receipt_revision),
        request_context=CTX,
    )


async def snapshot(memory, seed, scope=None):
    async with memory._pool.acquire() as conn:
        async with conn.transaction():
            await store.lock(conn, seed.bank)
            scope = scope or await store.discover(conn, seed.bank, [seed.raw])
            return await store.capture(conn, seed.bank, scope)


@pytest.mark.parametrize("table", ["documents", "chunks"])
async def test_source_writer_race_apply_is_rejected_without_mutation(memory, seeded, table):
    """A source change between phase-1 capture and phase-2 mutation must 409."""
    view = await preview(memory, seeded)
    request = manifest(view, correction=CurationFields(text="curation correction"))
    provider_started = asyncio.Event()
    allow_provider = asyncio.Event()
    writer_started = asyncio.Event()
    release_writer = asyncio.Event()

    async def provider(**kwargs):
        provider_started.set()
        await allow_provider.wait()
        return str([0.25] * 384)

    async def writer():
        column = "original_text" if table == "documents" else "chunk_text"
        async with memory._pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    f"UPDATE {table} SET {column}='ordinary writer changed provenance' WHERE bank_id=$1",
                    seeded.bank,
                )
                writer_started.set()
                await release_writer.wait()

    with patch.object(memory, "_reembed_memory_text", new=provider):
        apply_task = asyncio.create_task(apply(memory, seeded, request, f"source-race-{table}"))
        await asyncio.wait_for(provider_started.wait(), timeout=5)
        writer_task = asyncio.create_task(writer())
        await asyncio.wait_for(writer_started.wait(), timeout=5)
        allow_provider.set()
        try:
            with pytest.raises(CurationBatchConflict, match="busy"):
                await apply_task
        finally:
            release_writer.set()
            await writer_task

    async with memory._pool.acquire() as conn:
        assert await conn.fetchval("SELECT text FROM memory_units WHERE id=$1", seeded.raw) != "curation correction"
        assert await conn.fetchval("SELECT count(*) FROM curation_batches WHERE bank_id=$1", seeded.bank) == 0
        assert await conn.fetchval("SELECT count(*) FROM curation_entity_pins WHERE bank_id=$1", seeded.bank) == 0


async def test_exact_entity_observation_history_roundtrip(memory, seeded):
    before = await snapshot(memory, seeded)
    view = await preview(memory, seeded)
    assert view.inventory.observations == 1 and view.inventory.history_rows == 2
    req = manifest(view)
    with patch.object(memory, "submit_async_consolidation", new=AsyncMock()) as submit:
        receipt = await apply(memory, seeded, req)
        assert (
            receipt.maintenance_debt.consolidation
            and receipt.maintenance_debt.graph
            and receipt.maintenance_debt.model_refresh
        )
        async with memory._pool.acquire() as conn:
            assert (
                await conn.fetchval(
                    "SELECT count(*) FROM memory_units WHERE id=ANY($1::uuid[])", [seeded.raw, seeded.observation]
                )
                == 0
            )
            assert (
                await conn.fetchval(
                    "SELECT count(*) FROM observation_history WHERE observation_id=$1", seeded.observation
                )
                == 0
            )
            assert await conn.fetchval("SELECT count(*) FROM curation_entity_pins WHERE bank_id=$1", seeded.bank) == 2
            assert await conn.fetchval("SELECT consolidated_at IS NULL FROM memory_units WHERE id=$1", seeded.peer)
            assert await conn.fetchval("SELECT count(*) FROM async_operations WHERE bank_id=$1", seeded.bank) == 0
            assert await conn.fetchval("SELECT min(mention_count) FROM entities WHERE bank_id=$1", seeded.bank) == 0
        assert await memory.get_curation_batch(seeded.bank, "fixture", request_context=CTX) == receipt
        assert await apply(memory, seeded, req) == receipt
        undone = await revert(memory, seeded, receipt)
        assert undone.status == "reverted"
        assert await revert(memory, seeded, receipt) == undone
        assert await apply(memory, seeded, req) == undone
        submit.assert_not_called()
    after = await snapshot(memory, seeded, before.scope)
    assert snapshot_revision(after) == snapshot_revision(before)
    assert after.history.rows == before.history.rows
    async with memory._pool.acquire() as conn:
        assert await conn.fetchval("SELECT min(mention_count) FROM entities WHERE bank_id=$1", seeded.bank) == 2
        assert await conn.fetchval("SELECT count(*) FROM curation_entity_pins WHERE bank_id=$1", seeded.bank) == 0


async def test_entity_bearing_correction_dates_preserves_identity_and_embedding(memory, seeded):
    before = await snapshot(memory, seeded)
    receipt = await apply(
        memory,
        seeded,
        manifest(
            await preview(memory, seeded),
            correction=CurationFields(
                text="Alice retired the system",
                occurred_start=datetime(2021, 2, 3, tzinfo=UTC),
                occurred_end=datetime(2021, 2, 3, tzinfo=UTC),
            ),
        ),
    )
    async with memory._pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT text,occurred_start,event_date,edited_at FROM memory_units WHERE id=$1", seeded.raw
        )
        assert (
            row["text"] == "Alice retired the system"
            and row["occurred_start"].year == 2021
            and row["event_date"] == row["occurred_start"]
        )
        assert row["edited_at"] is not None
        assert await conn.fetchval("SELECT count(*) FROM unit_entities WHERE unit_id=$1", seeded.raw) == 2
        assert await conn.fetchval("SELECT min(mention_count) FROM entities WHERE bank_id=$1", seeded.bank) == 1
    await revert(memory, seeded, receipt)
    assert snapshot_revision(await snapshot(memory, seeded, before.scope)) == snapshot_revision(before)


async def test_queued_prune_keeps_pinned_orphans_and_cooccurrences(memory, seeded):
    # This claimant was queued BEFORE the capsule, as a real worker can be.
    async with memory._pool.acquire() as conn:
        await conn.executemany(
            "INSERT INTO entity_maintenance_queue(bank_id,entity_id) VALUES($1,$2)",
            [(seeded.bank, e) for e in seeded.entities],
        )
    receipt = await apply(memory, seeded, manifest(await preview(memory, seeded)))
    result = await graph.entity_prune_pass(backend=memory._backend, fq_table=fq_table, bank_id=seeded.bank)
    assert result.orphan_entities_pruned == 0 and result.stale_cooccurrences_pruned == 0
    async with memory._pool.acquire() as conn:
        assert await conn.fetchval("SELECT count(*) FROM entities WHERE bank_id=$1", seeded.bank) == 2
        assert (
            await conn.fetchval("SELECT count(*) FROM entity_cooccurrences WHERE entity_id_1=$1", seeded.entities[0])
            == 1
        )
    await revert(memory, seeded, receipt)


async def test_asymmetric_captured_cooccurrence_pin_protects_orphan_partner(memory, seeded):
    orphan = uuid.uuid4()
    left, right = sorted((seeded.entities[0], orphan))
    async with memory._pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO entities(id,bank_id,canonical_name,mention_count) VALUES($1,$2,'orphan partner',0)",
            orphan,
            seeded.bank,
        )
        await conn.execute(
            "INSERT INTO entity_cooccurrences(entity_id_1,entity_id_2,cooccurrence_count) VALUES($1,$2,1)",
            left,
            right,
        )
        await conn.execute("INSERT INTO entity_maintenance_queue(bank_id,entity_id) VALUES($1,$2)", seeded.bank, orphan)

    receipt = await apply(memory, seeded, manifest(await preview(memory, seeded)), "asymmetric-pin")
    result = await graph.entity_prune_pass(backend=memory._backend, fq_table=fq_table, bank_id=seeded.bank)
    assert result.orphan_entities_pruned == 0 and result.stale_cooccurrences_pruned == 0
    async with memory._pool.acquire() as conn:
        assert await conn.fetchval("SELECT 1 FROM entities WHERE id=$1", orphan)
        assert await conn.fetchval(
            "SELECT 1 FROM entity_cooccurrences WHERE entity_id_1=$1 AND entity_id_2=$2", left, right
        )
        assert await conn.fetchval(
            "SELECT 1 FROM curation_entity_pins WHERE bank_id=$1 AND batch_id=$2 AND entity_id=$3",
            seeded.bank,
            "asymmetric-pin",
            orphan,
        )
    assert (await revert(memory, seeded, receipt, "asymmetric-pin")).status == "reverted"
    result = await graph.entity_prune_pass(backend=memory._backend, fq_table=fq_table, bank_id=seeded.bank)
    assert result.orphan_entities_pruned == 1
    async with memory._pool.acquire() as conn:
        assert not await conn.fetchval("SELECT 1 FROM entities WHERE id=$1", orphan)
        assert not await conn.fetchval(
            "SELECT 1 FROM entity_cooccurrences WHERE entity_id_1=$1 AND entity_id_2=$2", left, right
        )


async def test_shared_entity_two_capsules_and_unrelated_posting_counter(memory, seeded):
    async with memory._pool.acquire() as conn:
        second = await raw(conn, seeded.bank, "Independent second target")
        await conn.execute("INSERT INTO unit_entities(unit_id,entity_id) VALUES($1,$2)", second, seeded.entities[0])
        await conn.execute("UPDATE entities SET mention_count=mention_count+1 WHERE id=$1", seeded.entities[0])
    first = await apply(memory, seeded, manifest(await preview(memory, seeded)), "first")
    second_receipt = await apply(memory, seeded, manifest(await preview(memory, seeded, [second])), "second")
    async with memory._pool.acquire() as conn:
        third = await raw(conn, seeded.bank, "Compatible unrelated addition")
        await conn.execute("INSERT INTO unit_entities(unit_id,entity_id) VALUES($1,$2)", third, seeded.entities[0])
        await conn.execute(
            "UPDATE entities SET mention_count=mention_count+1,last_seen=now() WHERE id=$1", seeded.entities[0]
        )
        await conn.execute(
            "UPDATE entity_cooccurrences SET cooccurrence_count=cooccurrence_count+1,last_cooccurred=now() "
            "WHERE entity_id_1=$1 AND entity_id_2=$2",
            *seeded.entities,
        )
    await revert(memory, seeded, first, "first")
    async with memory._pool.acquire() as conn:
        assert await conn.fetchval("SELECT mention_count FROM entities WHERE id=$1", seeded.entities[0]) == 3
        assert (
            await conn.fetchval("SELECT count(*) FROM curation_entity_pins WHERE entity_id=$1", seeded.entities[0]) == 1
        )
    await revert(memory, seeded, second_receipt, "second")
    async with memory._pool.acquire() as conn:
        assert await conn.fetchval("SELECT mention_count FROM entities WHERE id=$1", seeded.entities[0]) == 4
        assert (
            await conn.fetchval(
                "SELECT cooccurrence_count FROM entity_cooccurrences WHERE entity_id_1=$1 AND entity_id_2=$2",
                *seeded.entities,
            )
            == 3
        )


@pytest.mark.parametrize(
    "drift", ["raw", "source", "schema", "new-observation", "cosource-reconsolidated", "history-collision"]
)
async def test_revert_drift_conflicts_atomically_and_preserves_capsule(memory, seeded, drift):
    receipt = await apply(memory, seeded, manifest(await preview(memory, seeded)))
    async with memory._pool.acquire() as conn:
        if drift == "raw":
            await conn.execute("UPDATE invalidated_memory_units SET text='later edit' WHERE id=$1", seeded.raw)
        elif drift == "source":
            await conn.execute("UPDATE documents SET original_text='source changed' WHERE bank_id=$1", seeded.bank)
        elif drift == "schema":
            await conn.execute("ALTER TABLE memory_units ADD COLUMN v2_fixture_schema_drift boolean")
        elif drift == "new-observation":
            await conn.execute(
                "INSERT INTO memory_units(bank_id,text,fact_type,source_memory_ids,event_date) VALUES($1,'new dependency','observation',$2,now())",
                seeded.bank,
                [seeded.raw],
            )
        elif drift == "cosource-reconsolidated":
            await conn.execute("UPDATE memory_units SET consolidated_at=now() WHERE id=$1", seeded.peer)
        else:
            await conn.execute(
                "INSERT INTO observation_history(id,observation_id,bank_id,content) VALUES($1,$2,$3,'{}')",
                seeded.history[0],
                seeded.peer,
                seeded.bank,
            )
    try:
        with pytest.raises(CurationBatchConflict):
            await revert(memory, seeded, receipt)
        async with memory._pool.acquire() as conn:
            assert not await conn.fetchval("SELECT 1 FROM memory_units WHERE id=$1", seeded.raw)
            assert await conn.fetchval("SELECT status FROM curation_batches WHERE bank_id=$1", seeded.bank) == "applied"
            assert await conn.fetchval("SELECT count(*) FROM curation_entity_pins WHERE bank_id=$1", seeded.bank) == 2
    finally:
        if drift == "schema":
            async with memory._pool.acquire() as conn:
                await conn.execute("ALTER TABLE memory_units DROP COLUMN v2_fixture_schema_drift")


async def test_source_and_target_cas_before_apply_and_preparation_race(memory, seeded):
    req = manifest(await preview(memory, seeded), correction=CurationFields(text="replacement"))

    async def provider(**kwargs):
        async with memory._pool.acquire() as conn:
            await conn.execute(
                "UPDATE documents SET original_text='changed during provider work' WHERE bank_id=$1", seeded.bank
            )
        return str([0.25] * 384)

    with patch.object(memory, "_reembed_memory_text", new=provider):
        with pytest.raises(CurationBatchConflict, match="preparation"):
            await apply(memory, seeded, req)
    async with memory._pool.acquire() as conn:
        assert (
            await conn.fetchval("SELECT text FROM memory_units WHERE id=$1", seeded.raw)
            == "Alice works on a historical system"
        )
        assert await conn.fetchval("SELECT count(*) FROM curation_batches WHERE bank_id=$1", seeded.bank) == 0
    with pytest.raises(CurationBatchConflict, match="revision"):
        await apply(memory, seeded, req)


async def test_batch_atomicity_on_second_mutation_failure(memory, seeded):
    req = manifest(await preview(memory, seeded, [seeded.raw, seeded.peer]))
    original = store.writes.invalidate_memory
    calls = 0

    async def fail_second(**kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise CurationBatchConflict("injected SQL failure")
        return await original(**kwargs)

    before = await snapshot(memory, seeded)
    with patch.object(store.writes, "invalidate_memory", new=fail_second):
        with pytest.raises(CurationBatchConflict):
            await apply(memory, seeded, req)
    assert snapshot_revision(await snapshot(memory, seeded, before.scope)) == snapshot_revision(before)
    async with memory._pool.acquire() as conn:
        assert await conn.fetchval("SELECT count(*) FROM curation_batches WHERE bank_id=$1", seeded.bank) == 0
        assert await conn.fetchval("SELECT min(mention_count) FROM entities WHERE bank_id=$1", seeded.bank) == 2


async def test_active_capsule_blocks_bank_delete_and_identity_delete(memory, seeded):
    receipt = await apply(memory, seeded, manifest(await preview(memory, seeded)))
    with pytest.raises(CurationBatchConflict, match="capsule"):
        await memory.delete_bank(seeded.bank, request_context=CTX)
    async with memory._pool.acquire() as conn:
        with pytest.raises(asyncpg.RestrictViolationError):
            await conn.execute("DELETE FROM entities WHERE id=$1", seeded.entities[0])
    await revert(memory, seeded, receipt)


@pytest.mark.parametrize(
    "bad", ["unpaused", "active-operation", "transitive", "wrong-bank", "missing-provenance", "null-source"]
)
async def test_preview_fails_closed(memory, seeded, bad):
    ids = [seeded.raw]
    if bad == "unpaused":
        await memory.update_bank_config(seeded.bank, {"enable_auto_consolidation": True}, request_context=CTX)
    else:
        async with memory._pool.acquire() as conn:
            if bad == "active-operation":
                await conn.execute(
                    "INSERT INTO async_operations(bank_id,operation_type,status) VALUES($1,'consolidation','processing')",
                    seeded.bank,
                )
            elif bad == "transitive":
                await conn.execute(
                    "INSERT INTO memory_units(bank_id,text,fact_type,source_memory_ids,event_date) VALUES($1,'transitive','observation',$2,now())",
                    seeded.bank,
                    [seeded.observation],
                )
            elif bad == "wrong-bank":
                ids = [uuid.uuid4()]
            elif bad == "missing-provenance":
                await conn.execute("UPDATE memory_units SET chunk_id=NULL WHERE id=$1", seeded.raw)
            else:
                await conn.execute("UPDATE documents SET original_text=NULL WHERE bank_id=$1", seeded.bank)
    with pytest.raises(CurationBatchConflict):
        await preview(memory, seeded, ids)


async def test_bounded_source_content_and_observation_caps(memory, seeded, monkeypatch):
    monkeypatch.setattr(store, "MAX_BYTES", 1000)
    with pytest.raises(CurationBatchConflict, match="8MiB"):
        await preview(memory, seeded)
    monkeypatch.setattr(store, "MAX_BYTES", 8 * 1024 * 1024)
    monkeypatch.setattr(store, "MAX_OBSERVATIONS", 0)
    with pytest.raises(CurationBatchConflict, match="cap"):
        await preview(memory, seeded)


async def test_http_lost_ack_idempotence_manifest_conflict_and_cross_bank_lookup(memory, seeded):
    app = create_app(memory, initialize_memory=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        url = f"/v1/default/banks/{seeded.bank}/curation-batches"
        view = await client.post(
            url + "/preview", json={"protocol": "raw-curation-v2", "memory_ids": [str(seeded.raw)]}
        )
        assert view.status_code == 200, view.text
        req = manifest(await preview(memory, seeded)).model_dump(mode="json")
        applied = await client.post(url + "/lost-ack", json=req)
        assert applied.status_code == 200, applied.text
        loaded = await client.get(url + "/lost-ack")
        assert loaded.json() == applied.json()
        assert (await client.get("/v1/default/banks/another-bank/curation-batches/lost-ack")).status_code == 404
        req["changes"][0]["reason"] = "different manifest"
        assert (await client.post(url + "/lost-ack", json=req)).status_code == 409
        reversed_response = await client.post(
            url + "/lost-ack/revert",
            json={"protocol": "raw-curation-v2", "expected_receipt_revision": loaded.json()["receipt_revision"]},
        )
        assert reversed_response.status_code == 200, reversed_response.text
        assert reversed_response.json()["status"] == "reverted"


@pytest.mark.parametrize("clear_context", [False, True])
async def test_http_correction_presence_survives_capsule_get_retry_and_revert(memory, seeded, clear_context):
    async with memory._pool.acquire() as conn:
        await conn.execute(
            "UPDATE memory_units SET context='historical context' WHERE bank_id=$1 AND id=$2", seeded.bank, seeded.raw
        )
    before = await snapshot(memory, seeded)
    fields = CurationFields(text="replacement", context=None) if clear_context else CurationFields(text="replacement")
    expected_presence = {"text", "context"} if clear_context else {"text"}
    req = manifest(await preview(memory, seeded), correction=fields)
    wire = req.model_dump_json()
    parsed = CurationApplyRequest.model_validate_json(wire)
    assert parsed.changes[0].fields.model_fields_set == expected_presence
    clearing = parsed.model_copy(deep=True)
    clearing.changes[0].fields = (
        CurationFields(text="replacement") if clear_context else CurationFields(text="replacement", context=None)
    )
    assert revision(req) != revision(clearing)
    app = create_app(memory, initialize_memory=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        url = f"/v1/default/banks/{seeded.bank}/curation-batches/presence"
        applied = await client.post(url, content=wire, headers={"content-type": "application/json"})
        assert applied.status_code == 200, applied.text
        loaded = await client.get(url)
        assert loaded.status_code == 200, loaded.text
        assert loaded.json() == applied.json()
        repeated = await client.post(url, json=parsed.model_dump(mode="json"))
        assert repeated.status_code == 200, repeated.text
        assert repeated.json() == applied.json()
        conflict = await client.post(url, json=clearing.model_dump(mode="json"))
        assert conflict.status_code == 409, conflict.text
        async with memory._pool.acquire() as conn:
            assert await conn.fetchval(
                "SELECT context FROM memory_units WHERE bank_id=$1 AND id=$2", seeded.bank, seeded.raw
            ) == (None if clear_context else "historical context")
            capsule = await store.get_capsule(conn, seeded.bank, "presence")
            assert capsule.manifest.changes[0].fields.model_fields_set == expected_presence
            assert revision(capsule.manifest) == capsule.receipt.manifest_revision
            assert capsule.manifest.changes[0].reason == req.changes[0].reason
        undone = await client.post(
            url + "/revert",
            json={"protocol": "raw-curation-v2", "expected_receipt_revision": loaded.json()["receipt_revision"]},
        )
        assert undone.status_code == 200, undone.text
        assert undone.json()["status"] == "reverted"
        assert (await client.get(url)).json() == undone.json()
    assert snapshot_revision(await snapshot(memory, seeded, before.scope)) == snapshot_revision(before)


async def test_backup_restores_capsule_and_pins_with_conditional_revert(memory, seeded, pg0_db_url, tmp_path):
    from hindsight_api.admin.cli import _backup, _restore
    from hindsight_api.engine.memory_engine import _current_schema
    from hindsight_api.migrations import run_migrations

    before = await snapshot(memory, seeded)
    receipt = await apply(memory, seeded, manifest(await preview(memory, seeded)))
    backup_path = tmp_path / "capsule-backup.zip"
    backup = await _backup(pg0_db_url, backup_path)
    assert backup["tables"]["curation_batches"]["rows"] == 1
    assert backup["tables"]["curation_entity_pins"]["rows"] == 2
    schema = f"v2_recovery_{uuid.uuid4().hex[:12]}"
    async with memory._pool.acquire() as conn:
        await conn.execute(f'CREATE SCHEMA "{schema}"')
    try:
        run_migrations(pg0_db_url, schema=schema)
        await _restore(pg0_db_url, backup_path, schema=schema)
        token = _current_schema.set(schema)
        try:
            async with memory._pool.acquire() as conn:
                async with conn.transaction():
                    await store.lock(conn, seeded.bank)
                    capsule = await store.get_capsule(conn, seeded.bank, "fixture")
                    assert capsule.receipt == receipt
                    assert (
                        await conn.fetchval(
                            f'SELECT count(*) FROM "{schema}".curation_entity_pins WHERE bank_id=$1', seeded.bank
                        )
                        == 2
                    )
                    result = await store.revert(conn, seeded.bank, "fixture", capsule, receipt.receipt_revision)
                    assert result.status == "reverted"
                    restored = await store.capture(conn, seeded.bank, before.scope)
                    assert snapshot_revision(restored) == snapshot_revision(before)
        finally:
            _current_schema.reset(token)
    finally:
        async with memory._pool.acquire() as conn:
            await conn.execute(f'DROP SCHEMA "{schema}" CASCADE')


async def test_read_and_write_authorization_guards(memory, seeded):
    from types import SimpleNamespace

    from hindsight_api.extensions import ValidationResult

    validator = SimpleNamespace(
        validate_bank_read=AsyncMock(return_value=ValidationResult.reject("read denied", status_code=403)),
        validate_bank_write=AsyncMock(return_value=ValidationResult.reject("write denied", status_code=403)),
    )
    req = manifest(await preview(memory, seeded))
    with patch.object(memory, "_operation_validator", validator):
        app = create_app(memory, initialize_memory=False)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            url = f"/v1/default/banks/{seeded.bank}/curation-batches"
            assert (
                await client.post(
                    url + "/preview", json={"protocol": "raw-curation-v2", "memory_ids": [str(seeded.raw)]}
                )
            ).status_code == 403
            assert (await client.get(url + "/absent")).status_code == 403
            assert (await client.post(url + "/denied", json=req.model_dump(mode="json"))).status_code == 403
            assert (
                await client.post(
                    url + "/denied/revert", json={"protocol": "raw-curation-v2", "expected_receipt_revision": "0" * 64}
                )
            ).status_code == 403
    async with memory._pool.acquire() as conn:
        assert await conn.fetchval("SELECT count(*) FROM curation_batches WHERE bank_id=$1", seeded.bank) == 0


async def test_per_memory_validator_guards_batch_apply_and_revert_without_corruption(memory, seeded):
    from types import SimpleNamespace

    from hindsight_api.extensions import OperationValidationError, ValidationResult

    view = await preview(memory, seeded)
    request = manifest(view, correction=CurationFields(text="corrected raw"))
    before = await snapshot(memory, seeded)
    provider = AsyncMock(return_value=str([0.25] * 384))
    validator = SimpleNamespace(
        validate_bank_write=AsyncMock(return_value=ValidationResult.accept()),
        validate_memory_update=AsyncMock(
            return_value=ValidationResult.reject("memory curation denied", status_code=403)
        ),
    )
    with (
        patch.object(memory, "_reembed_memory_text", new=provider),
        patch.object(memory, "_operation_validator", validator),
    ):
        with pytest.raises(OperationValidationError) as denied_apply:
            await apply(memory, seeded, request)
    assert denied_apply.value.status_code == 403
    provider.assert_not_awaited()
    assert snapshot_revision(await snapshot(memory, seeded, before.scope)) == snapshot_revision(before)
    async with memory._pool.acquire() as conn:
        assert await conn.fetchval("SELECT count(*) FROM curation_batches WHERE bank_id=$1", seeded.bank) == 0

    receipt = await apply(memory, seeded, manifest(await preview(memory, seeded)), "guarded-revert")
    async with memory._pool.acquire() as conn:
        capsule_before = await conn.fetchval(
            "SELECT capsule::text FROM curation_batches WHERE bank_id=$1 AND batch_id=$2",
            seeded.bank,
            "guarded-revert",
        )
    with patch.object(memory, "_operation_validator", validator):
        with pytest.raises(OperationValidationError) as denied_revert:
            await memory.revert_curation_batch(
                seeded.bank,
                "guarded-revert",
                CurationRevertRequest(protocol="raw-curation-v2", expected_receipt_revision=receipt.receipt_revision),
                request_context=CTX,
            )
    assert denied_revert.value.status_code == 403
    async with memory._pool.acquire() as conn:
        assert (
            await conn.fetchval(
                "SELECT capsule::text FROM curation_batches WHERE bank_id=$1 AND batch_id=$2",
                seeded.bank,
                "guarded-revert",
            )
            == capsule_before
        )
        assert await conn.fetchval("SELECT 1 FROM invalidated_memory_units WHERE id=$1", seeded.raw)
        assert (
            await conn.fetchval(
                "SELECT status FROM curation_batches WHERE bank_id=$1 AND batch_id=$2", seeded.bank, "guarded-revert"
            )
            == "applied"
        )


@pytest.mark.parametrize("fields", [CurationFields(text="corrected raw"), CurationFields(context="corrected context")])
async def test_correction_revert_requires_edit_permission_but_invalidation_restore_does_not(memory, seeded, fields):
    from types import SimpleNamespace

    from hindsight_api.extensions import ValidationResult

    original = await snapshot(memory, seeded)
    original_text = next(r["text"] for r in original.memories.rows if r["id"] == str(seeded.raw))
    receipt = await apply(memory, seeded, manifest(await preview(memory, seeded), correction=fields), "edit-guard")
    corrected = await snapshot(memory, seeded, original.scope)
    async with memory._pool.acquire() as conn:
        capsule_before = await conn.fetchval(
            "SELECT capsule::text FROM curation_batches WHERE bank_id=$1 AND batch_id='edit-guard'", seeded.bank
        )

    def restore_only(ctx):
        return (
            ValidationResult.reject("field edits denied", status_code=403)
            if ctx.edits_fields
            else ValidationResult.accept()
        )

    validator = SimpleNamespace(
        validate_bank_write=AsyncMock(return_value=ValidationResult.accept()),
        validate_memory_update=AsyncMock(side_effect=restore_only),
    )
    app = create_app(memory, initialize_memory=False)
    url = f"/v1/default/banks/{seeded.bank}/curation-batches"
    with patch.object(memory, "_operation_validator", validator):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post(
                url + "/edit-guard/revert",
                json={"protocol": "raw-curation-v2", "expected_receipt_revision": receipt.receipt_revision},
            )
        assert response.status_code == 403, response.text
        assert "field edits denied" in response.json()["detail"]
    ctx = validator.validate_memory_update.await_args.args[0]
    assert ctx.edits_fields and ctx.text == original_text and ctx.state == "valid"
    assert snapshot_revision(await snapshot(memory, seeded, original.scope)) == snapshot_revision(corrected)
    async with memory._pool.acquire() as conn:
        assert (
            await conn.fetchval(
                "SELECT capsule::text FROM curation_batches WHERE bank_id=$1 AND batch_id='edit-guard'", seeded.bank
            )
            == capsule_before
        )
    await revert(memory, seeded, receipt, "edit-guard")

    invalidation = await apply(memory, seeded, manifest(await preview(memory, seeded)), "restore-guard")
    validator.validate_memory_update.reset_mock()
    with patch.object(memory, "_operation_validator", validator):
        assert (await revert(memory, seeded, invalidation, "restore-guard")).status == "reverted"
    ctx = validator.validate_memory_update.await_args.args[0]
    assert not ctx.edits_fields and ctx.text is None and ctx.state == "valid"
    assert snapshot_revision(await snapshot(memory, seeded, original.scope)) == snapshot_revision(original)


async def _add_incident_entity_partners(memory, seed, count):
    partners = [uuid.uuid4() for _ in range(count)]
    async with memory._pool.acquire() as conn:
        await conn.executemany(
            "INSERT INTO entities(id,bank_id,canonical_name,mention_count) VALUES($1,$2,$3,0)",
            [(partner, seed.bank, f"incident-{partner}") for partner in partners],
        )
        await conn.executemany(
            "INSERT INTO entity_cooccurrences(entity_id_1,entity_id_2,cooccurrence_count) VALUES($1,$2,1)",
            [sorted((seed.entities[0], partner)) for partner in partners],
        )


async def test_expanded_entity_pin_limit_admits_boundary_and_reports_actual_pins(memory, seeded):
    from hindsight_api.engine.curation_batch import MAX_ENTITIES

    await _add_incident_entity_partners(memory, seeded, MAX_ENTITIES - len(seeded.entities))
    view = await preview(memory, seeded)
    assert view.inventory.entities == MAX_ENTITIES
    receipt = await apply(memory, seeded, manifest(view), "pin-boundary")
    assert receipt.inventory.entities == MAX_ENTITIES
    async with memory._pool.acquire() as conn:
        assert (
            await conn.fetchval(
                "SELECT count(*) FROM curation_entity_pins WHERE bank_id=$1 AND batch_id='pin-boundary'", seeded.bank
            )
            == MAX_ENTITIES
        )
    assert (await revert(memory, seeded, receipt, "pin-boundary")).status == "reverted"


@pytest.mark.parametrize("stage", ["preview", "preparation"])
async def test_expanded_entity_pin_limit_rejects_overflow_before_mutation(memory, seeded, stage):
    from hindsight_api.engine.curation_batch import MAX_ENTITIES

    view = await preview(memory, seeded)
    original = await snapshot(memory, seeded)
    request = manifest(view, correction=CurationFields(text="corrected raw"))

    async def expand(*args, **kwargs):
        await _add_incident_entity_partners(memory, seeded, MAX_ENTITIES - len(seeded.entities) + 1)
        return str([0.25] * 384)

    if stage == "preview":
        await expand()
        with pytest.raises(CurationBatchConflict, match="Entity pin cap exceeded"):
            await preview(memory, seeded)
        with pytest.raises(CurationBatchConflict, match="Entity pin cap exceeded"):
            await apply(memory, seeded, request, "pin-overflow")
    else:
        with patch.object(memory, "_reembed_memory_text", new=expand):
            with pytest.raises(CurationBatchConflict, match="Entity pin cap exceeded"):
                await apply(memory, seeded, request, "pin-overflow")
    async with memory._pool.acquire() as conn:
        assert await conn.fetchval("SELECT count(*) FROM curation_batches WHERE bank_id=$1", seeded.bank) == 0
        assert await conn.fetchval("SELECT count(*) FROM curation_entity_pins WHERE bank_id=$1", seeded.bank) == 0
        assert await conn.fetchval("SELECT text FROM memory_units WHERE id=$1", seeded.raw) == next(
            r["text"] for r in original.memories.rows if r["id"] == str(seeded.raw)
        )
        assert await conn.fetchval("SELECT 1 FROM memory_units WHERE id=$1", seeded.observation)


async def test_non_postgres_and_non_sql_stores_fail_closed(memory, seeded):
    with patch.object(memory, "_database_backend_type", "oracle"):
        with pytest.raises(CurationBatchConflict, match="PostgreSQL"):
            await preview(memory, seeded)
    with patch.object(get_memories(), "store_owned_for", return_value=True):
        with pytest.raises(CurationBatchConflict, match="SQL-owned"):
            await preview(memory, seeded)


async def test_cross_bank_edges_are_rejected_before_any_cascade(memory, seeded):
    another = f"test-curation-v2-{uuid.uuid4().hex}"
    await memory.ensure_bank_profile(another, request_context=CTX)
    try:
        async with memory._pool.acquire() as conn:
            foreign = await raw(conn, another, "foreign peer")
            await conn.execute(
                "INSERT INTO memory_links(bank_id,from_unit_id,to_unit_id,link_type,weight) VALUES($1,$2,$3,'temporal',1)",
                another,
                seeded.raw,
                foreign,
            )
        with pytest.raises(CurationBatchConflict, match="Cross-bank"):
            await preview(memory, seeded)
        async with memory._pool.acquire() as conn:
            assert await conn.fetchval("SELECT 1 FROM memory_units WHERE id=$1", seeded.raw)
    finally:
        await memory.delete_bank(another, request_context=CTX)


@pytest.mark.parametrize("dependency", ["link", "observation", "both"])
@pytest.mark.parametrize("stage", ["preparation", "revert"])
async def test_cross_bank_dependency_races_conflict_without_mutation(memory, seeded, dependency, stage):
    another = f"test-curation-v2-{uuid.uuid4().hex}"
    await memory.ensure_bank_profile(another, request_context=CTX)

    # SQL is necessary to forge forbidden foreign dependencies and to compare
    # exact rows/capsules/pins; the public graph API deduplicates incident links.
    async def inject():
        async with memory._pool.acquire() as conn:
            foreign = await raw(conn, another, "foreign peer")
            if dependency in ("link", "both"):
                await conn.execute(
                    "INSERT INTO memory_links(bank_id,from_unit_id,to_unit_id,link_type,weight) VALUES($1,$2,$3,'temporal',1)",
                    another,
                    seeded.peer if stage == "revert" else seeded.raw,
                    foreign,
                )
            if dependency in ("observation", "both"):
                await conn.execute(
                    "INSERT INTO memory_units(bank_id,text,fact_type,source_memory_ids,event_date) VALUES($1,'foreign observation','observation',$2,now())",
                    another,
                    [seeded.raw],
                )

    async def exact_state():
        async with memory._pool.acquire() as conn:
            return {
                table: await conn.fetchval(
                    f"SELECT COALESCE(jsonb_agg(to_jsonb(r) ORDER BY to_jsonb(r)::text),'[]'::jsonb)::text FROM {table} r"
                )
                for table in store._TABLES
            }

    try:
        view = await preview(memory, seeded, [seeded.raw, seeded.peer])
        req = manifest(view)
        correction = next(change for change in req.changes if change.memory_id == seeded.peer)
        correction.action = "correct"
        correction.fields = CurationFields(text="corrected peer")
        if stage == "preparation":
            unchanged = None

            async def provider(**kwargs):
                nonlocal unchanged
                await inject()
                unchanged = await exact_state()
                return str([0.25] * 384)

            with patch.object(memory, "_reembed_memory_text", new=provider):
                with pytest.raises(CurationBatchConflict, match="Cross-bank"):
                    await apply(memory, seeded, req)
        else:
            receipt = await apply(memory, seeded, req)
            await inject()
            unchanged = await exact_state()
            with pytest.raises(CurationBatchConflict, match="Cross-bank"):
                await revert(memory, seeded, receipt)
        assert await exact_state() == unchanged
    finally:
        await memory.delete_bank(another, request_context=CTX)


async def test_cross_bank_cooccurrence_is_rejected_before_direct_or_http_apply(memory, seeded):
    another = f"test-curation-v2-{uuid.uuid4().hex}"
    await memory.ensure_bank_profile(another, request_context=CTX)
    await memory.update_bank_config(another, {"enable_auto_consolidation": False}, request_context=CTX)
    try:
        view = await preview(memory, seeded)
        request = manifest(view)
        foreign = uuid.uuid4()
        async with memory._pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO entities(id,bank_id,canonical_name,mention_count) VALUES($1,$2,'foreign entity',0)",
                foreign,
                another,
            )
            left, right = sorted((seeded.entities[0], foreign))
            await conn.execute(
                "INSERT INTO entity_cooccurrences(entity_id_1,entity_id_2,cooccurrence_count) VALUES($1,$2,1)",
                left,
                right,
            )
        with pytest.raises(CurationBatchConflict, match="Cooccurrence endpoint"):
            await apply(memory, seeded, request, "cross-bank-cooccurrence")
        app = create_app(memory, initialize_memory=False)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post(
                f"/v1/default/banks/{seeded.bank}/curation-batches/cross-bank-http",
                json=request.model_dump(mode="json"),
            )
        assert response.status_code == 409, response.text
        assert "Cooccurrence endpoint" in response.json()["detail"]
    finally:
        await memory.delete_bank(another, request_context=CTX)


async def test_lossless_canonical_numbers_are_deterministic():
    value = json.loads(
        '{"nested":[0.12345678901234567890123456789,-1e-30],"large":123456789012345678901234567890}',
        parse_float=Decimal,
    )
    snapshot = TableSnapshot(columns=[], rows=[value])
    with localcontext() as context:
        context.prec = 3
        encoded = canonical_bytes(snapshot)
    decoded = TableSnapshot.model_validate(json.loads(encoded, parse_float=Decimal))
    assert decoded.rows == snapshot.rows
    assert canonical_bytes(decoded) == encoded
    assert revision(value) == revision(dict(reversed(list(value.items()))))
    for nonfinite in (Decimal("NaN"), Decimal("Infinity")):
        with pytest.raises(ValueError, match="Nonfinite"):
            canonical_bytes(nonfinite)


@pytest.mark.parametrize("action", ["invalidate", "correct"])
async def test_high_precision_jsonb_roundtrip_and_numeric_drift(memory, seeded, action):
    precise = '{"precise":0.12345678901234567890123456789,"large":123456789012345678901234567890123456789}'
    drifted = precise.replace("0.12345678901234567890123456789", "0.12345678901234567891123456789")
    # Exactly the 20th fractional digit changes, below binary-float precision.
    assert [i for i, (a, b) in enumerate(zip(precise, drifted)) if a != b] == [precise.index("0.") + 21]
    # Compare PostgreSQL's own JSONB text, never an already-rounded Python
    # snapshot. History precision and hidden metadata cannot be read via recall.
    async with memory._pool.acquire() as conn:
        await conn.execute("UPDATE observation_history SET content=$2::jsonb WHERE id=$1", seeded.history[0], precise)
        await conn.execute(
            "UPDATE memory_units SET metadata=$2::jsonb WHERE id=ANY($1::uuid[])", [seeded.raw, seeded.peer], precise
        )
        original_history = await conn.fetchval(
            "SELECT content::text FROM observation_history WHERE id=$1", seeded.history[0]
        )
        original_metadata = await conn.fetchval("SELECT metadata::text FROM memory_units WHERE id=$1", seeded.peer)
    view = await preview(memory, seeded)
    req = manifest(view, correction=CurationFields(text="corrected raw") if action == "correct" else None)
    async with memory._pool.acquire() as conn:
        await conn.execute("UPDATE observation_history SET content=$2::jsonb WHERE id=$1", seeded.history[0], drifted)
    with pytest.raises(CurationBatchConflict, match="revision"):
        await apply(memory, seeded, req)
    async with memory._pool.acquire() as conn:
        await conn.execute("UPDATE observation_history SET content=$2::jsonb WHERE id=$1", seeded.history[0], precise)
    receipt = await apply(memory, seeded, req)
    async with memory._pool.acquire() as conn:
        await conn.execute("UPDATE memory_units SET metadata=$2::jsonb WHERE id=$1", seeded.peer, drifted)
        capsule = await conn.fetchval("SELECT capsule::text FROM curation_batches WHERE bank_id=$1", seeded.bank)
    with pytest.raises(CurationBatchConflict, match="closure changed"):
        await revert(memory, seeded, receipt)
    async with memory._pool.acquire() as conn:
        assert (
            await conn.fetchval("SELECT capsule::text FROM curation_batches WHERE bank_id=$1", seeded.bank) == capsule
        )
        assert await conn.fetchval("SELECT count(*) FROM curation_entity_pins WHERE bank_id=$1", seeded.bank) == 2
        await conn.execute("UPDATE memory_units SET metadata=$2::jsonb WHERE id=$1", seeded.peer, precise)
    await revert(memory, seeded, receipt)
    async with memory._pool.acquire() as conn:
        assert (
            await conn.fetchval("SELECT content::text FROM observation_history WHERE id=$1", seeded.history[0])
            == original_history
        )
        assert (
            await conn.fetchval("SELECT metadata::text FROM memory_units WHERE id=$1", seeded.peer) == original_metadata
        )
        assert (
            await conn.fetchval("SELECT metadata::text FROM memory_units WHERE id=$1", seeded.raw) == original_metadata
        )


async def test_combined_sources_are_bounded_before_snapshot_hashing(memory, seeded):
    ids = [seeded.raw]
    async with memory._pool.acquire() as conn:
        for index in range(5):
            mid = await raw(conn, seeded.bank, f"source-{index}")
            ids.append(mid)
            await conn.execute(
                "UPDATE documents SET original_text=$2 WHERE bank_id=$1 AND id=(SELECT document_id FROM memory_units WHERE id=$3)",
                seeded.bank,
                "x" * 900_000,
                mid,
            )
            await conn.execute(
                "UPDATE chunks SET chunk_text=$2 WHERE bank_id=$1 AND chunk_id=(SELECT chunk_id FROM memory_units WHERE id=$3)",
                seeded.bank,
                "x" * 900_000,
                mid,
            )
    with pytest.raises(CurationBatchConflict, match="Source content"):
        await preview(memory, seeded, ids)


async def test_large_snapshot_row_is_rejected_before_row_materialization(memory, seeded, monkeypatch):
    monkeypatch.setattr(store, "MAX_BYTES", 1000)
    async with memory._pool.acquire() as conn:
        await conn.execute(
            "UPDATE memory_units SET metadata=$2::jsonb WHERE bank_id=$1 AND id=$3",
            seeded.bank,
            json.dumps({"large": "x" * 5000}),
            seeded.raw,
        )
    with pytest.raises(CurationBatchConflict, match="Snapshot content"):
        await preview(memory, seeded)


async def test_oversized_source_is_rejected_before_hashing():
    class SourceConnection:
        def __init__(self):
            self.fetchrow_calls = 0

        async def fetchrow(self, query, *args):
            self.fetchrow_calls += 1
            if self.fetchrow_calls > 1:
                raise AssertionError("oversized source must not be hashed")
            return {
                "id": "document-id",
                "content_hash": "fixture",
                "updated_at": datetime.now(UTC),
                "chunk_id": "chunk-id",
                "document_bytes": 1024 * 1024 + 1,
                "chunk_bytes": 1,
            }

    conn = SourceConnection()
    with pytest.raises(CurationBatchConflict, match="1MiB"):
        await store.source_revision(conn, "bank", {"document_id": "document-id", "chunk_id": "chunk-id"})
    assert conn.fetchrow_calls == 1


async def test_snapshot_byte_bound_is_checked_before_fetching_rows():
    class SnapshotConnection:
        def __init__(self):
            self.data_fetches = 0

        async def fetch(self, query, *args):
            if "pg_attribute" in query:
                return []
            self.data_fetches += 1
            raise AssertionError("oversized snapshot rows must not be transferred")

        async def fetchrow(self, query, *args):
            assert "row_count" in query and "octet_length(row)" in query
            return {"row_count": 1, "row_bytes": store.MAX_BYTES + 1}

    conn = SnapshotConnection()
    with pytest.raises(CurationBatchConflict, match="Snapshot content"):
        await store.table_snapshot(conn, "memory_units", "SELECT '{}' AS row", cap=1)
    assert conn.data_fetches == 0


@pytest.mark.parametrize("cap", ["MAX_ENTITIES", "MAX_LINKS", "MAX_HISTORY", "MAX_PEERS"])
async def test_every_closure_row_family_has_a_cap(memory, seeded, monkeypatch, cap):
    monkeypatch.setattr(store, cap, 1 if cap == "MAX_HISTORY" else 0)
    with pytest.raises(CurationBatchConflict, match="cap"):
        await preview(memory, seeded)


async def test_busy_curation_window_fails_without_waiting_or_mutation(memory, seeded):
    async with memory._pool.acquire() as blocker:
        async with blocker.transaction():
            await store.lock(blocker, seeded.bank)
            with pytest.raises(CurationBatchConflict, match="busy"):
                await preview(memory, seeded)
    async with memory._pool.acquire() as conn:
        assert await conn.fetchval("SELECT count(*) FROM curation_batches WHERE bank_id=$1", seeded.bank) == 0


async def test_curation_lock_does_not_block_unrelated_bank_writes(memory, seeded):
    another = f"test-curation-v2-{uuid.uuid4().hex}"
    await memory.ensure_bank_profile(another, request_context=CTX)
    await memory.update_bank_config(another, {"enable_auto_consolidation": False}, request_context=CTX)
    try:
        async with memory._pool.acquire() as blocker:
            async with blocker.transaction():
                await store.lock(blocker, seeded.bank)
                scope = await store.discover(blocker, seeded.bank, [seeded.raw])
                await store.lock_closure(blocker, seeded.bank, scope)
                async with memory._pool.acquire() as writer:
                    async with writer.transaction():
                        await raw(writer, another, "unrelated bank write")
        async with memory._pool.acquire() as conn:
            assert (
                await conn.fetchval(
                    "SELECT count(*) FROM memory_units WHERE bank_id=$1 AND text='unrelated bank write'", another
                )
                == 1
            )
    finally:
        await memory.delete_bank(another, request_context=CTX)


async def test_same_bank_writer_race_apply_is_rejected_without_overwrite(memory, seeded):
    view = await preview(memory, seeded)
    request = manifest(view, correction=CurationFields(text="curation correction"))
    provider_started = asyncio.Event()
    allow_provider = asyncio.Event()

    async def provider(**kwargs):
        provider_started.set()
        await allow_provider.wait()
        return str([0.25] * 384)

    async def writer():
        async with memory._pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    "UPDATE memory_units SET text='ordinary writer' WHERE bank_id=$1 AND id=$2",
                    seeded.bank,
                    seeded.raw,
                )
                writer_started.set()
                await release_writer.wait()

    writer_started = asyncio.Event()
    release_writer = asyncio.Event()
    with patch.object(memory, "_reembed_memory_text", new=provider):
        apply_task = asyncio.create_task(apply(memory, seeded, request, "same-bank-apply"))
        await asyncio.wait_for(provider_started.wait(), timeout=5)
        writer_task = asyncio.create_task(writer())
        await asyncio.wait_for(writer_started.wait(), timeout=5)
        allow_provider.set()
        try:
            with pytest.raises(CurationBatchConflict, match="busy"):
                await apply_task
        finally:
            release_writer.set()
            await writer_task

    async with memory._pool.acquire() as conn:
        assert await conn.fetchval("SELECT text FROM memory_units WHERE id=$1", seeded.raw) == "ordinary writer"
        assert await conn.fetchval("SELECT count(*) FROM curation_batches WHERE bank_id=$1", seeded.bank) == 0


async def test_same_bank_writer_race_revert_is_rejected_without_overwrite(memory, seeded):
    receipt = await apply(memory, seeded, manifest(await preview(memory, seeded)), "same-bank-revert")
    writer_started = asyncio.Event()
    release_writer = asyncio.Event()

    async def writer():
        async with memory._pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    "UPDATE invalidated_memory_units SET text='ordinary archive writer' WHERE bank_id=$1 AND id=$2",
                    seeded.bank,
                    seeded.raw,
                )
                writer_started.set()
                await release_writer.wait()

    writer_task = asyncio.create_task(writer())
    await asyncio.wait_for(writer_started.wait(), timeout=5)
    try:
        with pytest.raises(CurationBatchConflict, match="busy"):
            await revert(memory, seeded, receipt, "same-bank-revert")
    finally:
        release_writer.set()
        await writer_task

    async with memory._pool.acquire() as conn:
        assert (
            await conn.fetchval("SELECT text FROM invalidated_memory_units WHERE id=$1", seeded.raw)
            == "ordinary archive writer"
        )
        assert (
            await conn.fetchval(
                "SELECT status FROM curation_batches WHERE bank_id=$1 AND batch_id=$2", seeded.bank, "same-bank-revert"
            )
            == "applied"
        )


async def test_lost_ack_after_commit_is_reconciled_by_receipt_get(memory, seeded):
    request = manifest(await preview(memory, seeded))
    with patch.object(
        memory._bank_stats_cache,
        "invalidate",
        new=AsyncMock(side_effect=RuntimeError("cache unavailable after commit")),
    ):
        with pytest.raises(RuntimeError, match="after commit"):
            await apply(memory, seeded, request, "lost-after-commit")
    committed = await memory.get_curation_batch(seeded.bank, "lost-after-commit", request_context=CTX)
    assert committed.status == "applied"
    assert await apply(memory, seeded, request, "lost-after-commit") == committed
    await revert(memory, seeded, committed, "lost-after-commit")
