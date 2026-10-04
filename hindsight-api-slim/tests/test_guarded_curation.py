"""Guarded PATCH rejects stale evidence and preserves dependent observations."""

import asyncio
import hashlib
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from unittest.mock import patch

import pytest
import pytest_asyncio

from hindsight_api.engine import bank_info_cache
from hindsight_api.engine.chunk_ids import build_chunk_id
from hindsight_api.engine.curation_guard import (
    CurationGuard,
    CurationSourceSnapshot,
    memory_snapshot_sha256,
    snapshot_sha256,
)
from hindsight_api.engine.memories import get_memories
from hindsight_api.engine.memories.pg import graph as pg_graph
from hindsight_api.engine.schema import fq_table

pytestmark = [pytest.mark.asyncio, pytest.mark.memory_backend_incompatible]


@dataclass
class Case:
    bank: str
    memory_id: str
    document_id: str
    chunk_id: str
    source: str

    @property
    def path(self) -> str:
        return f"/v1/default/banks/{self.bank}/memories/{self.memory_id}"


@pytest_asyncio.fixture
async def case(memory, request_context):
    bank = "guard-" + uuid.uuid4().hex
    document = "source"
    chunk = build_chunk_id(bank, document, 0)
    unit = uuid.uuid4()
    source = "The assistant proposed a migration, which has not happened."
    await memory.ensure_bank_profile(bank_id=bank, request_context=request_context)
    async with memory._backend.acquire() as conn:
        await conn.execute("UPDATE banks SET config = '{\"enable_auto_consolidation\":false}' WHERE bank_id = $1", bank)
        await conn.execute(
            "INSERT INTO documents(id,bank_id,original_text,content_hash) VALUES ($1,$2,$3,'source-hash')",
            document,
            bank,
            source,
        )
        await conn.execute(
            "INSERT INTO chunks(chunk_id,document_id,bank_id,chunk_index,chunk_text) VALUES ($1,$2,$3,0,$4)",
            chunk,
            document,
            bank,
            source,
        )
        await conn.execute(
            "INSERT INTO memory_units(id,bank_id,text,fact_type,event_date,document_id,chunk_id) "
            "VALUES ($1,$2,'The migration happened.','world',$3,$4,$5)",
            unit,
            bank,
            datetime(2026, 9, 30, tzinfo=UTC),
            document,
            chunk,
        )
    yield Case(bank, str(unit), document, chunk, source)
    await memory.delete_bank(bank_id=bank, request_context=request_context)


async def guard_for(client, case: Case) -> CurationGuard:
    response = await client.get(case.path)
    assert response.status_code == 200
    fact = response.json()
    doc_response = await client.get(f"/v1/default/banks/{case.bank}/documents/{case.document_id}")
    assert doc_response.status_code == 200
    doc = doc_response.json()
    source = CurationSourceSnapshot(
        document_id=case.document_id,
        content_hash=doc["content_hash"],
        updated_at=doc["updated_at"],
        original_text_sha256=hashlib.sha256(case.source.encode()).hexdigest(),
        chunk_id=case.chunk_id,
        chunk_text_sha256=hashlib.sha256(case.source.encode()).hexdigest(),
    )
    return CurationGuard(
        protocol="raw-curation-v1",
        expected_memory_sha256=memory_snapshot_sha256(fact),
        expected_source_sha256=snapshot_sha256(source),
        require_no_observations=True,
        require_quiescent_consolidation=True,
    )


@pytest.mark.parametrize("guarded", [True, False])
async def test_invalidate_and_restore_round_trip_honors_uncached_pause(
    api_client, case, memory, request_context, guarded
):
    # Another API process pauses the bank without invalidating this process's
    # warm cache. Both guarded and ordinary curation must honor that pause.
    async with memory._backend.acquire() as conn:
        await conn.execute(
            "UPDATE banks SET config = '{\"enable_auto_consolidation\":true}' WHERE bank_id = $1", case.bank
        )
    await bank_info_cache.invalidate(case.bank, "config")
    assert (await memory._config_resolver.resolve_full_config(case.bank, request_context)).enable_auto_consolidation
    async with memory._backend.acquire() as conn:
        await conn.execute(
            "UPDATE banks SET config = '{\"enable_auto_consolidation\":false}' WHERE bank_id = $1", case.bank
        )
    assert (await memory._config_resolver.resolve_full_config(case.bank, request_context)).enable_auto_consolidation

    before = (await api_client.get(case.path)).json()
    guard = await guard_for(api_client, case)
    with patch.object(memory, "submit_async_consolidation") as submit:
        response = await api_client.patch(
            case.path,
            json={
                "state": "invalidated",
                "reason": "batch:test, unsupported outcome",
                **({"curation_guard": guard.model_dump()} if guarded else {}),
            },
        )
        assert response.status_code == 200, response.text
        assert response.json()["state"] == "invalidated"
        archived_guard = await guard_for(api_client, case)
        restored = await api_client.patch(
            case.path,
            json={"state": "valid", **({"curation_guard": archived_guard.model_dump()} if guarded else {})},
        )
        assert restored.status_code == 200, restored.text
        submit.assert_not_awaited()
    after = (await api_client.get(case.path)).json()
    for field in ("text", "context", "type", "date", "entities", "document_id", "chunk_id", "tags", "metadata"):
        assert after[field] == before[field]
    assert after["state"] == "valid"


@pytest.mark.parametrize(
    "drift", ["text", "document", "chunk", "dependency", "configuration", "operation", "oversized"]
)
async def test_guard_rejects_evidence_or_safety_drift(api_client, case, memory, drift):
    guard = await guard_for(api_client, case)
    observation_id = uuid.uuid4()
    async with memory._backend.acquire() as conn:
        if drift == "text":
            await conn.execute(
                "UPDATE memory_units SET text='A newer correction' WHERE bank_id=$1 AND id=$2",
                case.bank,
                case.memory_id,
            )
        elif drift == "document":
            await conn.execute(
                "UPDATE documents SET original_text='Newer source' WHERE bank_id=$1 AND id=$2",
                case.bank,
                case.document_id,
            )
        elif drift == "chunk":
            await conn.execute(
                "UPDATE chunks SET chunk_text='Newer chunk' WHERE bank_id=$1 AND chunk_id=$2", case.bank, case.chunk_id
            )
        elif drift == "dependency":
            await conn.execute(
                "INSERT INTO memory_units(id,bank_id,text,fact_type,source_memory_ids) "
                "VALUES ($1,$2,'Preserve this derived observation','observation',$3::uuid[])",
                observation_id,
                case.bank,
                [uuid.UUID(case.memory_id)],
            )
        elif drift == "configuration":
            await conn.execute(
                "UPDATE banks SET config='{\"enable_auto_consolidation\":true}' WHERE bank_id=$1", case.bank
            )
        elif drift == "oversized":
            await conn.execute(
                "UPDATE documents SET original_text=repeat('x',1048577) WHERE bank_id=$1 AND id=$2",
                case.bank,
                case.document_id,
            )
        else:
            await conn.execute(
                "INSERT INTO async_operations(operation_id,bank_id,operation_type,status) "
                "VALUES ($1,$2,'consolidation','processing')",
                uuid.uuid4(),
                case.bank,
            )
    response = await api_client.patch(case.path, json={"state": "invalidated", "curation_guard": guard.model_dump()})
    assert response.status_code == 409, response.text
    assert (await api_client.get(case.path)).json()["state"] == "valid"
    if drift == "dependency":
        async with memory._backend.acquire() as conn:
            assert await conn.fetchval(
                "SELECT text FROM memory_units WHERE bank_id=$1 AND id=$2", case.bank, observation_id
            )


async def test_busy_write_window_fails_without_waiting_or_mutation(api_client, case, memory):
    guard = await guard_for(api_client, case)
    async with memory._backend.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "UPDATE documents SET content_hash=content_hash WHERE bank_id=$1 AND id=$2", case.bank, case.document_id
            )
            response = await api_client.patch(
                case.path, json={"state": "invalidated", "curation_guard": guard.model_dump()}
            )
            assert response.status_code == 409, response.text
    assert (await api_client.get(case.path)).json()["state"] == "valid"


@pytest.mark.parametrize("action", ["invalidate", "edit", "restore", "reason"])
async def test_row_lock_contention_returns_prompt_conflict_and_rolls_back(api_client, case, memory, action):
    if action in ("restore", "reason"):
        guard = await guard_for(api_client, case)
        response = await api_client.patch(
            case.path, json={"state": "invalidated", "curation_guard": guard.model_dump()}
        )
        assert response.status_code == 200, response.text
    before = (await api_client.get(case.path)).json()
    guard = await guard_for(api_client, case)
    table = "invalidated_memory_units" if action in ("restore", "reason") else "memory_units"
    changes = {
        "invalidate": {"state": "invalidated"},
        "edit": {"text": "The migration was only proposed."},
        "restore": {"state": "valid"},
        "reason": {"state": "invalidated", "reason": "A newer reason"},
    }[action]
    with patch.object(memory, "submit_async_consolidation") as submit:
        async with memory._backend.acquire() as conn:
            async with conn.transaction():
                # FOR UPDATE only takes a ROW SHARE table lock, compatible with
                # the guard's table locks. The later write must still fail fast.
                await conn.fetchrow(
                    f"SELECT id FROM {table} WHERE bank_id=$1 AND id=$2 FOR UPDATE", case.bank, case.memory_id
                )
                response = await asyncio.wait_for(
                    api_client.patch(case.path, json={**changes, "curation_guard": guard.model_dump()}), timeout=1.0
                )
                assert response.status_code == 409, response.text
                assert "busy" in response.json()["detail"]
                # Read while the competing row lock is still held: the guard
                # transaction has rolled back, not waited for the lock release.
                assert (await api_client.get(case.path)).json() == before
                # GET prefers the live row and cannot expose a partial duplicate
                # left in the archive (or vice versa). Check both stores too.
                assert await conn.fetchval(
                    "SELECT count(*) FROM memory_units WHERE bank_id=$1 AND id=$2", case.bank, case.memory_id
                ) == (0 if action in ("restore", "reason") else 1)
                assert await conn.fetchval(
                    "SELECT count(*) FROM invalidated_memory_units WHERE bank_id=$1 AND id=$2",
                    case.bank,
                    case.memory_id,
                ) == (1 if action in ("restore", "reason") else 0)
        submit.assert_not_awaited()


async def test_guard_is_advertised_and_entity_changes_rejected(api_client, case):
    spec = (await api_client.get("/openapi.json")).json()
    assert "curation_guard" in spec["components"]["schemas"]["UpdateMemoryRequest"]["properties"]
    assert "409" in spec["paths"]["/v1/default/banks/{bank_id}/memories/{memory_id}"]["patch"]["responses"]
    guard = await guard_for(api_client, case)
    response = await api_client.patch(
        case.path,
        json={
            "entities": ["Invented Person"],
            "curation_guard": guard.model_dump(),
        },
    )
    assert response.status_code == 409, response.text
    assert (await api_client.get(case.path)).json()["entities"] == []


async def test_edit_rechecks_after_off_transaction_embedding(api_client, case, memory):
    guard = await guard_for(api_client, case)

    async def edit_during_embedding(**kwargs):
        async with memory._backend.acquire() as conn:
            await conn.execute(
                "UPDATE memory_units SET text='A concurrent authoritative correction' WHERE bank_id=$1 AND id=$2",
                case.bank,
                case.memory_id,
            )
        return None

    with patch.object(memory, "_reembed_memory_text", side_effect=edit_during_embedding):
        response = await api_client.patch(
            case.path,
            json={
                "text": "The migration was only proposed.",
                "curation_guard": guard.model_dump(),
            },
        )
    assert response.status_code == 409, response.text
    current = (await api_client.get(case.path)).json()
    assert current["text"] == "A concurrent authoritative correction"
    assert current["state"] == "valid"


async def test_relink_read_cannot_insert_stale_link_after_guarded_edit(api_client, case, memory, request_context):
    """A relink read racing a guarded edit must serialize, not resurrect a deleted edge."""
    victim_id = uuid.uuid4()
    async with memory._backend.acquire() as conn:
        await conn.execute(
            "INSERT INTO memory_units(id,bank_id,text,fact_type,event_date) VALUES ($1,$2,'victim','world',$3)",
            victim_id,
            case.bank,
            datetime(2026, 9, 29, tzinfo=UTC),
        )
        await conn.execute(
            "INSERT INTO memory_links(from_unit_id,to_unit_id,link_type,weight,bank_id) "
            "VALUES ($1,$2,'temporal',0.5,$3)",
            victim_id,
            uuid.UUID(case.memory_id),
            case.bank,
        )
        await conn.execute("INSERT INTO graph_maintenance_queue(bank_id,unit_id) VALUES ($1,$2)", case.bank, victim_id)

    read_done = asyncio.Event()
    release_relink = asyncio.Event()

    async def paused_relink_batch(conn, table_fn, bank_id, victim_ids, ops, backend):
        row = await conn.fetchrow(
            "SELECT from_unit_id,to_unit_id,link_type,weight FROM memory_links WHERE bank_id=$1 AND from_unit_id=$2",
            bank_id,
            victim_ids[0],
        )
        assert row is not None
        read_done.set()
        await release_relink.wait()
        await conn.execute(
            "INSERT INTO memory_links(from_unit_id,to_unit_id,link_type,weight,bank_id) "
            "VALUES ($1,$2,$3,$4,$5) ON CONFLICT DO NOTHING",
            row["from_unit_id"],
            row["to_unit_id"],
            row["link_type"],
            row["weight"],
            bank_id,
        )
        return 1

    backend = await memory._get_backend()
    store = get_memories()
    config = await memory._config_resolver.resolve_full_config(case.bank, request_context)
    relink_task = None
    guard = await guard_for(api_client, case)
    with (
        patch.object(pg_graph, "_relink_batch", side_effect=paused_relink_batch),
        patch.object(memory, "submit_async_graph_maintenance", return_value=None),
    ):
        relink_task = asyncio.create_task(
            store.relink_pass(backend=backend, fq_table=fq_table, bank_id=case.bank, config=config)
        )
        await asyncio.wait_for(read_done.wait(), timeout=2.0)
        response = await api_client.patch(
            case.path,
            json={"text": "The migration was only proposed.", "curation_guard": guard.model_dump()},
        )
        assert response.status_code == 409, response.text
        release_relink.set()
        await asyncio.wait_for(relink_task, timeout=2.0)

    async with memory._backend.acquire() as conn:
        assert (
            await conn.fetchval("SELECT text FROM memory_units WHERE bank_id=$1 AND id=$2", case.bank, case.memory_id)
            == "The migration happened."
        )
        assert (
            await conn.fetchval(
                "SELECT count(*) FROM memory_links WHERE bank_id=$1 AND from_unit_id=$2 AND to_unit_id=$3",
                case.bank,
                victim_id,
                uuid.UUID(case.memory_id),
            )
            == 1
        )
