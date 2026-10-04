"""Original uploads survive retention when their bank preserves source evidence.

Both banks go through real upload, conversion, worker retention and background
consolidation. The supported bank export provides the operation's storage key,
then the published download client proves whether the original bytes survived.
"""

from __future__ import annotations

import io
import json
import uuid
import zipfile
from collections.abc import AsyncIterator

import pytest
from hindsight_client_api.api.document_transfer_api import DocumentTransferApi

from hindsight_system_tests.payloads import consolidation, extracted, fact

pytestmark = pytest.mark.asyncio

CONTENT = b"Alice keeps a blue notebook for her cello practice."


@pytest.fixture
async def other_bank(client) -> AsyncIterator[str]:
    name = f"systest-{uuid.uuid4().hex[:12]}"
    await client.acreate_bank(name)
    yield name
    await client.banks.delete_bank(name)


async def _original_key(client, bank_id: str) -> str:
    archive = await client.aexport_bank(bank_id, poll_interval=0.1)
    with zipfile.ZipFile(io.BytesIO(archive)) as zipped:
        operations = json.loads(zipped.read("data/async_operations.json"))
    conversions = [row for row in operations if row["operation_type"] == "file_convert_retain"]
    assert len(conversions) == 1
    payload = conversions[0]["task_payload"]
    if isinstance(payload, str):
        payload = json.loads(payload)
    return payload["storage_key"]


async def _download(client, key: str) -> bytes:
    api = DocumentTransferApi(client.documents.api_client)
    response = await api.download_file_without_preload_content(key)
    assert response.status == 200
    return bytes(await response.read())


async def test_bank_preservation_keeps_originals_without_changing_other_banks(
    client, llm, bank_id, other_bank, settled, tmp_path
):
    llm.on_step("extract_facts").returns(
        extracted(fact("Alice keeps a blue notebook for cello practice", who="Alice", entities=["Alice"]))
    )
    llm.on_step("consolidate").returns(consolidation())
    await client.acreate_bank(bank_id)
    await client.banks.update_bank_config(bank_id, {"updates": {"file_delete_after_retain": False}})
    source = tmp_path / "notebook.txt"
    source.write_bytes(CONTENT)
    for bank in (bank_id, other_bank):
        receipt = await client.aretain_files(
            bank, [source], files_metadata=[{"document_id": "notebook", "parser": ["markitdown"]}]
        )
        assert len(receipt.operation_ids) == 1
        await settled(bank)
        status = await client.operations.get_operation_status(bank, receipt.operation_ids[0])
        assert status.status == "completed"
        document = await client.documents.get_document(bank, "notebook")
        assert document.original_text == CONTENT.decode()
        assert document.memory_unit_count == 1
        memories = await client.memory.list_memories(bank, limit=100)
        assert all(memory.document_id == "notebook" for memory in memories.items)

    assert await _download(client, await _original_key(client, bank_id)) == CONTENT
    api = DocumentTransferApi(client.documents.api_client)
    missing = await api.download_file_without_preload_content(await _original_key(client, other_bank))
    assert missing.status == 404
    await missing.read()
    assert (await client.banks.get_bank_config(other_bank)).config["file_delete_after_retain"] is True

    # Clearing the override restores inheritance for future files. It does not
    # sweep previously preserved originals.
    await client.banks.update_bank_config(bank_id, {"updates": {"file_delete_after_retain": None}})
    assert (await client.banks.get_bank_config(bank_id)).config["file_delete_after_retain"] is True
    assert await _download(client, await _original_key(client, bank_id)) == CONTENT
