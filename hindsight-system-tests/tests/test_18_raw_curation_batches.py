"""A bounded batch retracts derived claims and restores their exact identities.

The corpus is created by real retain/consolidation work, then the public client
opens an explicitly paused curation window. Recovery is visible to a caller,
without submitting new background jobs or resolving replacement entity IDs.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from hindsight_client_api.exceptions import ApiException

from hindsight_system_tests.payloads import Observation, consolidation, extracted, fact, fact_ids_in

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def observed_bank(client, llm, bank_id, settled):
    await client.acreate_bank(bank_id=bank_id)
    await client.banks.update_bank_config(bank_id, {"updates": {"enable_auto_consolidation": True}})
    llm.on_step("extract_facts").returns(
        extracted(
            fact("Alice moved to Berlin", who="Alice", entities=["Alice", "Berlin"]),
            fact("Alice renewed her Berlin lease", who="Alice", entities=["Alice", "Berlin"]),
        )
    )
    llm.on_step("consolidate").answers_with(
        lambda request: consolidation(
            creates=[Observation(text="Alice is settled in Berlin", source_fact_ids=fact_ids_in(request.user_text))]
        )
    )
    await client.aretain(
        bank_id=bank_id,
        content="Alice moved to Berlin. Alice renewed her Berlin lease.",
        document_id="canonical",
        context="historical lease context",
    )
    await settled(bank_id)
    await client.banks.update_bank_config(bank_id, {"updates": {"enable_auto_consolidation": False}})
    await settled(bank_id)
    return bank_id


def _change(target, action, *, fields=None):
    from hindsight_client_api.models.curation_change import CurationChange

    values = target.to_dict()
    values.update(action=action, reason="canonical evidence correction")
    if fields is not None:
        values["fields"] = fields
    return CurationChange(**values)


async def _operations(client, bank):
    listing = await client.operations.list_operations(bank, limit=100)
    return {operation.id: operation.status for operation in listing.operations}


async def test_durable_batch_roundtrip_restores_raw_and_observation_ids(client, observed_bank, settled):
    from hindsight_client_api.models.curation_apply_request import CurationApplyRequest
    from hindsight_client_api.models.curation_preview_request import CurationPreviewRequest
    from hindsight_client_api.models.curation_revert_request import CurationRevertRequest

    bank = observed_bank
    before = (await client.memory.list_memories(bank, limit=100)).items
    raw = next(memory for memory in before if "moved to Berlin" in memory.text)
    observation = next(memory for memory in before if memory.fact_type == "observation")
    entities_before = (await client.entities.list_entities(bank, limit=100)).items
    assert len(before) == 3 and entities_before
    ops_before = await _operations(client, bank)

    preview = await client.memory.preview_curation_batch(
        bank, CurationPreviewRequest(protocol="raw-curation-v2", memory_ids=[raw.id])
    )
    assert preview.inventory.observations == 1 and preview.inventory.entities > 0
    request = CurationApplyRequest(
        protocol="raw-curation-v2",
        expected_closure_revision=preview.closure_revision,
        changes=[_change(preview.targets[0], "invalidate")],
    )
    receipt = await client.memory.apply_curation_batch(bank, "withdraw-historical-fact", request)
    assert receipt.status == "applied" and receipt.maintenance_debt.consolidation
    await settled(bank)
    remaining = (await client.memory.list_memories(bank, limit=100)).items
    assert raw.id not in {memory.id for memory in remaining}
    assert observation.id not in {memory.id for memory in remaining}
    assert await _operations(client, bank) == ops_before
    assert (await client.memory.get_curation_batch(bank, receipt.batch_id)).to_dict() == receipt.to_dict()
    assert (await client.memory.apply_curation_batch(bank, receipt.batch_id, request)).to_dict() == receipt.to_dict()

    with pytest.raises(ApiException) as blocked:
        await client.banks.delete_bank(bank)
    assert blocked.value.status == 409

    restored = await client.memory.revert_curation_batch(
        bank,
        receipt.batch_id,
        CurationRevertRequest(protocol="raw-curation-v2", expected_receipt_revision=receipt.receipt_revision),
    )
    assert restored.status == "reverted"
    after = (await client.memory.list_memories(bank, limit=100)).items
    assert {memory.id: memory.text for memory in after} == {memory.id: memory.text for memory in before}
    entities_after = (await client.entities.list_entities(bank, limit=100)).items
    assert {entity.id: entity.canonical_name for entity in entities_after} == {
        entity.id: entity.canonical_name for entity in entities_before
    }
    assert await _operations(client, bank) == ops_before
    assert (await client.memory.apply_curation_batch(bank, receipt.batch_id, request)).status == "reverted"


async def test_published_client_keeps_omitted_and_explicit_null_fields_distinct(client, observed_bank):
    from hindsight_client_api.models.curation_apply_request import CurationApplyRequest
    from hindsight_client_api.models.curation_fields import CurationFields
    from hindsight_client_api.models.curation_preview_request import CurationPreviewRequest
    from hindsight_client_api.models.curation_revert_request import CurationRevertRequest

    bank = observed_bank
    target = next(
        memory
        for memory in (await client.memory.list_memories(bank, limit=100)).items
        if "moved to Berlin" in memory.text
    )
    original = await client.memory.get_memory(bank, target.id)
    assert original["context"] == "historical lease context"
    view = await client.memory.preview_curation_batch(
        bank, CurationPreviewRequest(protocol="raw-curation-v2", memory_ids=[target.id])
    )
    fields = CurationFields(
        text="Alice retired her former Berlin setup",
        context=None,
        occurred_start=datetime(2020, 1, 2, tzinfo=timezone.utc),
        occurred_end=datetime(2020, 1, 2, tzinfo=timezone.utc),
    )
    assert "fact_type" not in fields.to_dict() and fields.to_dict()["context"] is None
    req = CurationApplyRequest(
        protocol="raw-curation-v2",
        expected_closure_revision=view.closure_revision,
        changes=[_change(view.targets[0], "correct", fields=fields)],
    )
    receipt = await client.memory.apply_curation_batch(bank, "correct-event-date", req)
    assert (await client.memory.get_curation_batch(bank, receipt.batch_id)).to_dict() == receipt.to_dict()
    assert (await client.memory.apply_curation_batch(bank, receipt.batch_id, req)).to_dict() == receipt.to_dict()
    omitted_context = CurationFields.from_dict(
        {key: value for key, value in fields.to_dict().items() if key != "context"}
    )
    conflicting = CurationApplyRequest(
        protocol="raw-curation-v2",
        expected_closure_revision=view.closure_revision,
        changes=[_change(view.targets[0], "correct", fields=omitted_context)],
    )
    with pytest.raises(ApiException) as conflict:
        await client.memory.apply_curation_batch(bank, receipt.batch_id, conflicting)
    assert conflict.value.status == 409
    detail = await client.memory.get_memory(bank, target.id)
    assert detail["text"] == "Alice retired her former Berlin setup"
    # The existing public memory detail route represents cleared SQL context as
    # an empty string. Core PostgreSQL proofs additionally assert stored NULL.
    assert detail["context"] == "" and detail["occurred_start"].startswith("2020-01-02")
    await client.memory.revert_curation_batch(
        bank,
        receipt.batch_id,
        CurationRevertRequest(protocol="raw-curation-v2", expected_receipt_revision=receipt.receipt_revision),
    )
    detail = await client.memory.get_memory(bank, target.id)
    assert detail["text"] == target.text
    assert detail["context"] == original["context"]
    assert (await client.memory.get_curation_batch(bank, receipt.batch_id)).status == "reverted"
    assert (await client.memory.apply_curation_batch(bank, receipt.batch_id, req)).status == "reverted"
