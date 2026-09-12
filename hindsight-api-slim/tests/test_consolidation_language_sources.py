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
        union_source_facts={},
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
async def test_unpersistable_recalled_citation_rejects_the_whole_response_before_language_check(
    monkeypatch: pytest.MonkeyPatch, config: SimpleNamespace
) -> None:
    """A recalled-only citation cannot authorize a sibling action that is written."""
    llm = AsyncMock()
    llm._provider_impl = None
    llm.call.return_value = LLMCallResult(
        content=SimpleNamespace(
            creates=[
                SimpleNamespace(text="valid sibling", source_fact_ids=["new-source"]),
                SimpleNamespace(text="invalid citation", source_fact_ids=["recalled-only"]),
            ],
            updates=[],
            deletes=[],
        ),
        usage=TokenUsage(),
    )

    async def prepare(*_args: object, **_kwargs: object) -> object:
        return object()

    async def evaluate(*_args: object, **_kwargs: object) -> LanguageCheckResult:
        raise AssertionError("invalid response must be rejected before language authority is evaluated")

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

    assert result.failed
    assert not result.creates
    assert llm.call.await_count == 1


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
        union_source_facts={},
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
