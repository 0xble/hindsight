"""Regression coverage for consolidation language authority from original chunks."""

from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest

from hindsight_api.engine.chunk_ids import build_chunk_id
from hindsight_api.engine.consolidation import consolidator
from hindsight_api.engine.consolidation.consolidator import _consolidate_batch_with_llm, _resolve_original_source_texts
from hindsight_api.engine.db import DatabaseBackend
from hindsight_api.engine.language_integrity import LanguageCheckResult
from hindsight_api.engine.memories.base import StoredMemory
from hindsight_api.engine.response_models import LLMCallResult, MemoryFact, TokenUsage


@pytest.fixture
def config() -> SimpleNamespace:
    return SimpleNamespace(
        llm_output_language=None,
        llm_language_integrity="observe",
        observations_mission=None,
        llm_strict_schema_consolidation=False,
        llm_supports_max_items=False,
        consolidation_max_attempts=1,
        consolidation_llm_max_retries=None,
        consolidation_max_completion_tokens=None,
        consolidation_max_context_tokens=100_000,
        llm_temperature_consolidation=0.0,
    )


@pytest.mark.asyncio
async def test_update_language_validation_uses_original_chunk_texts_for_new_and_prior_sources(
    monkeypatch: pytest.MonkeyPatch, config: SimpleNamespace
) -> None:
    """An update is accountable to both its new fact and the observation's prior evidence."""
    llm = AsyncMock()
    llm._provider_impl = None
    llm.call.return_value = LLMCallResult(
        content=SimpleNamespace(
            creates=[],
            updates=[
                SimpleNamespace(
                    text="用户在深圳湾公园遛猫。",
                    observation_id="observation-1",
                    source_fact_ids=["new-source"],
                )
            ],
            deletes=[],
        ),
        usage=TokenUsage(),
    )
    prepared_sources: list[dict[str, str]] = []
    generated_groups: list[tuple[str, ...]] = []

    async def prepare(source_texts: dict[str, str], **_kwargs: object) -> object:
        prepared_sources.append(source_texts)
        return object()

    async def evaluate(context: object, generated, **_kwargs: object) -> LanguageCheckResult:
        assert context is not None
        generated_groups.extend(item.source_keys for item in generated)
        return LanguageCheckResult(mismatches=(), checked=1, abstained=0)

    monkeypatch.setattr(consolidator, "prepare_context_safely", prepare)
    monkeypatch.setattr(consolidator, "evaluate_language_integrity_safely", evaluate)

    result = await _consolidate_batch_with_llm(
        llm_config=llm,
        memories=[{"id": "new-source", "text": "translated fact text must not become language authority"}],
        union_observations=[
            MemoryFact(
                id="observation-1",
                text="translated observation text",
                fact_type="observation",
                source_fact_ids=["prior-source"],
            )
        ],
        # Complete derived lineage lets this test exercise language authority;
        # omitted provenance is separately required to veto UPDATEs fail closed.
        union_source_facts={
            "prior-source": MemoryFact(id="prior-source", text="translated prior fact text", fact_type="world")
        },
        original_source_text_by_id={
            "new-source": "用户最近在深圳湾公园遛猫。",
            "prior-source": "用户周末经常带宠物去公园散步。",
        },
        config=config,
    )

    assert result.updates
    assert prepared_sources == [
        {
            "new-source": "用户最近在深圳湾公园遛猫。",
            "prior-source": "用户周末经常带宠物去公园散步。",
        }
    ]
    assert generated_groups == [("new-source", "prior-source")]


@pytest.mark.asyncio
async def test_missing_original_source_text_does_not_fall_back_to_fact_text(
    monkeypatch: pytest.MonkeyPatch, config: SimpleNamespace
) -> None:
    """Missing chunk content remains unknown rather than treating derived facts as originals."""
    llm = AsyncMock()
    llm._provider_impl = None
    llm.call.return_value = LLMCallResult(
        content=SimpleNamespace(creates=[], updates=[], deletes=[]),
        usage=TokenUsage(),
    )
    prepared_sources: list[dict[str, str]] = []

    async def prepare(source_texts: dict[str, str], **_kwargs: object) -> object:
        prepared_sources.append(source_texts)
        return object()

    monkeypatch.setattr(consolidator, "prepare_context_safely", prepare)

    await _consolidate_batch_with_llm(
        llm_config=llm,
        memories=[{"id": "missing-source", "text": "derived English fact text"}],
        union_observations=[],
        union_source_facts={},
        original_source_text_by_id={},
        config=config,
    )

    assert prepared_sources == [{}]


@pytest.mark.asyncio
async def test_unpersistable_recalled_citation_drops_only_that_action_before_language_check(
    monkeypatch: pytest.MonkeyPatch, config: SimpleNamespace
) -> None:
    """A recalled-only citation is dropped as a whole action and never reaches language authority.

    The valid sibling keeps writing, because every source it cites is persisted as validated.
    """
    llm = AsyncMock()
    llm._provider_impl = None
    llm.call.return_value = LLMCallResult(
        content=SimpleNamespace(
            creates=[
                SimpleNamespace(text="valid sibling", source_fact_ids=["new-source"]),
                SimpleNamespace(text="invalid citation", source_fact_ids=["new-source", "recalled-only"]),
            ],
            updates=[],
            deletes=[],
        ),
        usage=TokenUsage(),
    )
    seen: list[tuple[str, tuple[str, ...]]] = []

    async def prepare(*_args: object, **_kwargs: object) -> object:
        return object()

    async def evaluate(_context: object, generated, **_kwargs: object) -> LanguageCheckResult:
        seen.extend((item.text, item.source_keys) for item in generated)
        return LanguageCheckResult(mismatches=(), checked=1, abstained=0)

    monkeypatch.setattr(consolidator, "prepare_context_safely", prepare)
    monkeypatch.setattr(consolidator, "evaluate_language_integrity_safely", evaluate)

    result = await _consolidate_batch_with_llm(
        llm_config=llm,
        memories=[{"id": "new-source", "text": "new fact"}],
        union_observations=[],
        union_source_facts={"recalled-only": cast(MemoryFact, SimpleNamespace())},
        original_source_text_by_id={"new-source": "original chunk", "recalled-only": "unrelated chunk"},
        config=config,
    )

    assert not result.failed
    assert [create.text for create in result.creates] == ["valid sibling"]
    assert seen == [("valid sibling", ("new-source",))]
    assert llm.call.await_count == 1


@pytest.mark.asyncio
async def test_fact_cited_only_by_dropped_actions_remains_pending(
    monkeypatch: pytest.MonkeyPatch, config: SimpleNamespace
) -> None:
    """A batch fact whose only citing action is dropped must not be stamped into nothing."""
    llm = AsyncMock()
    llm._provider_impl = None
    llm.call.return_value = LLMCallResult(
        content=SimpleNamespace(
            creates=[
                SimpleNamespace(text="valid sibling", source_fact_ids=["A"]),
                SimpleNamespace(text="invalid citation", source_fact_ids=["B", "recalled-only"]),
            ],
            updates=[],
            deletes=[],
        ),
        usage=TokenUsage(),
    )

    async def prepare(*_args: object, **_kwargs: object) -> object:
        return object()

    async def evaluate(_context: object, generated, **_kwargs: object) -> LanguageCheckResult:
        assert [item.text for item in generated] == ["valid sibling"]
        return LanguageCheckResult(mismatches=(), checked=1, abstained=0)

    monkeypatch.setattr(consolidator, "prepare_context_safely", prepare)
    monkeypatch.setattr(consolidator, "evaluate_language_integrity_safely", evaluate)

    result = await _consolidate_batch_with_llm(
        llm_config=llm,
        memories=[{"id": "A", "text": "fact A"}, {"id": "B", "text": "fact B"}],
        union_observations=[],
        union_source_facts={},
        original_source_text_by_id={"A": "original A", "B": "original B"},
        config=config,
    )

    assert not result.failed
    assert [action.text for action in result.creates] == ["valid sibling"]
    assert result.pending_fact_ids == {"B"}
    assert llm.call.await_count == 1


def test_filter_reports_each_rule_and_keeps_valid_actions() -> None:
    response = consolidator._ConsolidationBatchResponse.model_construct(
        creates=[
            SimpleNamespace(text="ok", source_fact_ids=["A"]),
            SimpleNamespace(text="outside", source_fact_ids=["A", "X"]),
        ],
        updates=[
            SimpleNamespace(text="ok", observation_id="O", source_fact_ids=["B"]),
            SimpleNamespace(text="no source", observation_id="O", source_fact_ids=[]),
            SimpleNamespace(text="outside", observation_id="O", source_fact_ids=["B", "X"]),
        ],
        deletes=[SimpleNamespace(observation_id="O")],
    )

    result = consolidator._filter_unpersistable_references(
        response,
        memories=[{"id": "A"}, {"id": "B"}],
        union_observations=[MemoryFact(id="O", text="observation", fact_type="observation", source_fact_ids=[])],
        per_fact_observation_ids={"A": set(), "B": {"O"}},
    )

    assert [a.text for a in result.response.creates] == ["ok"]
    assert [a.text for a in result.response.updates] == ["ok"]
    assert [a.observation_id for a in result.response.deletes] == ["O"]
    assert result.dropped == {
        "create_cites_fact_outside_batch": 1,
        "update_without_sources": 1,
        "update_cites_fact_outside_batch": 1,
    }
    assert result.pending_fact_ids == set()
    assert result.unsafe_delete
    assert result.must_reject


def test_filter_drops_sourceless_create_without_rejecting_valid_sibling() -> None:
    response = consolidator._ConsolidationBatchResponse.model_construct(
        creates=[
            SimpleNamespace(text="valid", source_fact_ids=["A"]),
            SimpleNamespace(text="no source", source_fact_ids=[]),
        ],
        updates=[],
        deletes=[],
    )
    result = consolidator._filter_unpersistable_references(response, memories=[{"id": "A"}], union_observations=[])
    assert result.dropped == {"create_without_sources": 1}
    assert not result.must_reject
    assert [action.text for action in result.response.creates] == ["valid"]


def test_filter_rejects_unknown_only_sources_amid_valid_siblings() -> None:
    response = consolidator._ConsolidationBatchResponse.model_construct(
        creates=[
            SimpleNamespace(text="valid", source_fact_ids=["A"]),
            SimpleNamespace(text="unknown source", source_fact_ids=["hallucinated-B"]),
        ],
        updates=[],
        deletes=[],
    )
    result = consolidator._filter_unpersistable_references(
        response,
        memories=[{"id": "A"}, {"id": "B"}],
        union_observations=[],
    )
    assert result.dropped == {"create_cites_fact_outside_batch": 1}
    assert result.must_reject


def test_invalid_update_and_delete_targets_reject_whole_reply() -> None:
    observation = MemoryFact(id="O", text="old", fact_type="observation", source_fact_ids=[])
    for response in (
        consolidator._ConsolidationBatchResponse.model_construct(
            creates=[SimpleNamespace(text="valid", source_fact_ids=["A"])],
            updates=[SimpleNamespace(text="unsafe", observation_id="Z", source_fact_ids=["B"])],
            deletes=[],
        ),
        consolidator._ConsolidationBatchResponse.model_construct(
            creates=[SimpleNamespace(text="valid", source_fact_ids=["A"])],
            updates=[SimpleNamespace(text="unsafe", observation_id="O", source_fact_ids=["B"])],
            deletes=[],
        ),
        consolidator._ConsolidationBatchResponse.model_construct(
            creates=[SimpleNamespace(text="valid", source_fact_ids=["A"])],
            updates=[],
            deletes=[SimpleNamespace(observation_id="Z")],
        ),
    ):
        with pytest.raises(consolidator._InvalidConsolidationReferences):
            consolidator._filter_unpersistable_references(
                response,
                memories=[{"id": "A"}, {"id": "B"}],
                union_observations=[observation],
                per_fact_observation_ids={"A": {"O"}, "B": set()},
            )


def test_filter_keeps_deletes_when_nothing_was_dropped() -> None:
    response = consolidator._ConsolidationBatchResponse.model_construct(
        creates=[SimpleNamespace(text="merged", source_fact_ids=["A"])],
        updates=[],
        deletes=[SimpleNamespace(observation_id="O")],
    )

    result = consolidator._filter_unpersistable_references(
        response,
        memories=[{"id": "A"}],
        union_observations=[MemoryFact(id="O", text="observation", fact_type="observation", source_fact_ids=[])],
    )

    assert not result.dropped
    assert not result.must_reject
    assert [a.observation_id for a in result.response.deletes] == ["O"]


@pytest.mark.asyncio
async def test_update_must_be_recalled_for_one_of_its_cited_sources_before_language_check(
    monkeypatch: pytest.MonkeyPatch, config: SimpleNamespace
) -> None:
    """Union membership cannot replace the per-cited-source recall topology."""
    llm = AsyncMock()
    llm._provider_impl = None
    llm.call.return_value = LLMCallResult(
        content=SimpleNamespace(
            creates=[SimpleNamespace(text="valid sibling", source_fact_ids=["B"])],
            updates=[SimpleNamespace(text="invalid update", observation_id="O", source_fact_ids=["A"])],
            deletes=[SimpleNamespace(observation_id="O")],
        ),
        usage=TokenUsage(),
    )

    async def prepare(*_args: object, **_kwargs: object) -> object:
        return object()

    async def evaluate(*_args: object, **_kwargs: object) -> LanguageCheckResult:
        raise AssertionError("invalid response must be rejected before language evaluation")

    monkeypatch.setattr(consolidator, "prepare_context_safely", prepare)
    monkeypatch.setattr(consolidator, "evaluate_language_integrity_safely", evaluate)

    result = await _consolidate_batch_with_llm(
        llm_config=llm,
        memories=[{"id": "A", "text": "fact A"}, {"id": "B", "text": "fact B"}],
        union_observations=[MemoryFact(id="O", text="observation", fact_type="observation", source_fact_ids=[])],
        union_source_facts={},
        original_source_text_by_id={"A": "original A", "B": "original B"},
        per_fact_observation_ids={"A": set(), "B": {"O"}},
        config=config,
    )

    assert result.failed
    assert not result.creates
    assert not result.updates
    assert not result.deletes
    assert llm.call.await_count == 1


@pytest.mark.asyncio
async def test_update_recalled_for_a_cited_source_and_prior_provenance_remains_valid(
    monkeypatch: pytest.MonkeyPatch, config: SimpleNamespace
) -> None:
    llm = AsyncMock()
    llm._provider_impl = None
    llm.call.return_value = LLMCallResult(
        content=SimpleNamespace(
            creates=[],
            updates=[SimpleNamespace(text="valid update", observation_id="O", source_fact_ids=["B"])],
            deletes=[],
        ),
        usage=TokenUsage(),
    )
    seen: list[tuple[str, ...]] = []

    async def prepare(*_args: object, **_kwargs: object) -> object:
        return object()

    async def evaluate(_context: object, generated, **_kwargs: object) -> LanguageCheckResult:
        seen.extend(item.source_keys for item in generated)
        return LanguageCheckResult(mismatches=(), checked=1, abstained=0)

    monkeypatch.setattr(consolidator, "prepare_context_safely", prepare)
    monkeypatch.setattr(consolidator, "evaluate_language_integrity_safely", evaluate)

    result = await _consolidate_batch_with_llm(
        llm_config=llm,
        memories=[{"id": "A", "text": "fact A"}, {"id": "B", "text": "fact B"}],
        union_observations=[MemoryFact(id="O", text="observation", fact_type="observation", source_fact_ids=["prior"])],
        union_source_facts={"prior": MemoryFact(id="prior", text="prior derived fact", fact_type="world")},
        original_source_text_by_id={"A": "original A", "B": "original B", "prior": "prior original"},
        per_fact_observation_ids={"A": set(), "B": {"O"}},
        config=config,
    )

    assert result.updates
    assert seen == [("B", "prior")]


@pytest.mark.asyncio
async def test_original_sources_are_resolved_bank_scoped_from_store_chunks(monkeypatch: pytest.MonkeyPatch) -> None:
    """New, recalled-union, and prior-observation sources all resolve from their chunks."""
    bank_id = "bank-a"
    source_ids = {"new-source", "union-source", "prior-source"}
    seen: dict[str, object] = {}

    class Store:
        def store_owned_for(self, requested_bank_id: str) -> bool:
            assert requested_bank_id == bank_id
            return True

        async def get_memories(self, **kwargs: Any) -> list[StoredMemory]:
            seen["bank_id"] = kwargs["bank_id"]
            seen["unit_ids"] = set(kwargs["unit_ids"])
            return [
                StoredMemory(
                    unit_id=source_id,
                    text="derived fact text is not used",
                    fact_type="world",
                    chunk_id=build_chunk_id(bank_id, f"document-{source_id}", 0),
                )
                for source_id in source_ids
            ]

        async def get_chunk_texts(self, *, bank_id: str, refs: list[tuple[str, int]]) -> list[str | None]:
            assert bank_id == "bank-a"
            return [f"original text for {document_id}" for document_id, _ in refs]

    monkeypatch.setattr(consolidator, "get_memories", lambda: Store())

    resolved = await _resolve_original_source_texts(
        pool=cast(DatabaseBackend, object()), bank_id=bank_id, source_ids=source_ids
    )

    assert seen == {"bank_id": bank_id, "unit_ids": source_ids}
    assert resolved == {source_id: f"original text for document-{source_id}" for source_id in source_ids}
