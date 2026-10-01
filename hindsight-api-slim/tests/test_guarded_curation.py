"""Guarded PATCH rejects stale evidence and preserves dependent observations."""

import hashlib
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from unittest.mock import patch

import pytest
import pytest_asyncio

from hindsight_api.engine.chunk_ids import build_chunk_id
from hindsight_api.engine.curation_guard import (
    CurationGuard,
    CurationSourceSnapshot,
    memory_snapshot_sha256,
    snapshot_sha256,
)

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


async def test_invalidate_and_restore_guarded_round_trip(api_client, case):
    before = (await api_client.get(case.path)).json()
    guard = await guard_for(api_client, case)
    response = await api_client.patch(
        case.path,
        json={
            "state": "invalidated",
            "reason": "batch:test, unsupported outcome",
            "curation_guard": guard.model_dump(),
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["state"] == "invalidated"
    archived_guard = await guard_for(api_client, case)
    restored = await api_client.patch(case.path, json={"state": "valid", "curation_guard": archived_guard.model_dump()})
    assert restored.status_code == 200, restored.text
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


async def test_guard_is_advertised_and_entity_changes_rejected(api_client, case):
    spec = (await api_client.get("/openapi.json")).json()
    assert "curation_guard" in spec["components"]["schemas"]["UpdateMemoryRequest"]["properties"]
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
