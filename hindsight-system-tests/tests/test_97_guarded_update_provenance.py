"""A rejected lossy UPDATE adds its evidence without changing a supported detail.

An ordinary CREATE in the same reply can fold into an exact twin. The rejected
UPDATE's fallback must still keep its own row and citations, even with that same
text, so successful duplicate reconciliation cannot silently undo the guard.
"""

from __future__ import annotations

import pytest

from hindsight_system_tests.payloads import (
    Consolidation,
    Observation,
    ObservationDelete,
    ObservationUpdate,
    extracted,
    fact,
    fact_ids_in,
    observes,
)
from hindsight_system_tests.rulebook import ChatRequest

pytestmark = pytest.mark.asyncio

BEFORE = "Server timeout is 5 seconds."
AFTER = "Server is monitored."


async def test_lossy_update_and_exact_create_retain_every_rounds_evidence(client, llm, bank_id, settled) -> None:
    llm.on_step("extract_facts").returns(extracted(fact(BEFORE, entities=["Server"])))
    llm.on_step("consolidate").answers_with(observes(BEFORE))
    await client.aretain(bank_id=bank_id, content=BEFORE, document_id="guard-original")
    await settled(bank_id)
    original = next(
        m for m in (await client.memory.list_memories(bank_id, limit=100)).items if m.fact_type == "observation"
    )

    llm.reset()
    llm.on_step("extract_facts").returns(extracted(fact(AFTER, entities=["Server"])))
    llm.on_step("consolidate").answers_with(observes(AFTER))
    await client.aretain(bank_id=bank_id, content=AFTER, document_id="guard-exact-twin")
    await settled(bank_id)
    second = await client.memory.list_memories(bank_id, limit=100)
    twin = next(m for m in second.items if m.fact_type == "observation" and m.text == AFTER)

    llm.reset()
    llm.on_step("extract_facts").returns(
        extracted(fact("Server is monitored by an independent health check.", entities=["Server"]))
    )

    def propose(request: ChatRequest) -> Consolidation:
        sources = fact_ids_in(request.all_text)
        return Consolidation(
            creates=[Observation(text=AFTER, source_fact_ids=sources)],
            updates=[ObservationUpdate(observation_id=original.id, text=AFTER, source_fact_ids=sources)],
            deletes=[ObservationDelete(observation_id=original.id)],
        )

    # A correction deliberately repeats the same loss. It is a scripted provider
    # response, so the real guard and shared attempt budget decide the fallback.
    llm.on_step("consolidate").answers_with(propose)
    await client.aretain(
        bank_id=bank_id,
        content="Server is monitored by an independent health check.",
        document_id="guard-independent-source",
    )
    await settled(bank_id)
    final = await client.memory.list_memories(bank_id, limit=100)
    by_id = {m.id: m for m in final.items}
    observations = [m for m in final.items if m.fact_type == "observation"]
    assert len(observations) == 3
    assert by_id[original.id].text == BEFORE
    assert by_id[original.id].source_memory_ids == original.source_memory_ids
    assert by_id[twin.id].text == AFTER and by_id[twin.id].proof_count == 2
    fallback = next(m for m in observations if m.id not in {original.id, twin.id})
    assert fallback.text == AFTER and fallback.proof_count == 1
    new_sources = set(by_id[twin.id].source_memory_ids) - set(twin.source_memory_ids)
    assert set(fallback.source_memory_ids) == new_sources
    assert len(new_sources) == 1
    for observation in observations:
        assert observation.source_memory_ids
        assert all(
            source in by_id and by_id[source].fact_type != "observation" for source in observation.source_memory_ids
        )
