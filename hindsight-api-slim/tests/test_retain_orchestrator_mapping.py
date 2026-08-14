"""Unit tests for retain orchestrator mapping and embeddings length guarantee.

Regression coverage for issue #1037: a silent length mismatch between the
extracted facts and the generated embeddings caused
`_map_results_to_contents` to raise IndexError during batch_retain.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from hindsight_api.engine import memories as memories_mod
from hindsight_api.engine.memories.postgres import PostgresMemories
from hindsight_api.engine.retain import (
    embedding_utils,
    entity_processing,
    fact_storage,
    link_creation,
    link_utils,
    orchestrator,
)
from hindsight_api.engine.retain.orchestrator import (
    _insert_facts_and_links,
    _map_results_to_contents,
    _pre_resolve_phase1,
    _process_extracted_facts,
    _remap_causal_relations,
    _run_final_semantic_ann,
)
from hindsight_api.engine.retain.types import (
    CausalRelation,
    EntityResolutionResult,
    ExtractedFact,
    ProcessedFact,
    RetainContent,
)


def _make_processed_fact(content_index: int, text: str = "fact") -> ProcessedFact:
    return ProcessedFact(
        fact_text=text,
        fact_type="world",
        embedding=[0.0, 0.0, 0.0],
        occurred_start=None,
        occurred_end=None,
        mentioned_at=datetime(2026, 1, 1),
        context="",
        metadata={},
        content_index=content_index,
    )


def _make_content(text: str = "x") -> RetainContent:
    return RetainContent(content=text)


def _make_extracted_fact(text: str, chunk_index: int, causal_targets: list[int] | None = None) -> ExtractedFact:
    return ExtractedFact(
        fact_text=text,
        fact_type="world",
        chunk_index=chunk_index,
        mentioned_at=datetime(2026, 1, 1),
        causal_relations=[
            CausalRelation(relation_type="caused_by", target_fact_index=target) for target in causal_targets or []
        ],
    )


class TestMapResultsToContents:
    def test_groups_unit_ids_by_content_index(self):
        contents = [_make_content("a"), _make_content("b"), _make_content("c")]
        processed = [
            _make_processed_fact(0, "a1"),
            _make_processed_fact(0, "a2"),
            _make_processed_fact(2, "c1"),
        ]
        unit_ids = ["u-a1", "u-a2", "u-c1"]

        result = _map_results_to_contents(contents, processed, unit_ids)

        assert result == [["u-a1", "u-a2"], [], ["u-c1"]]

    def test_handles_out_of_range_content_index(self):
        contents = [_make_content("a"), _make_content("b")]
        processed = [
            _make_processed_fact(-1, "f1"),
            _make_processed_fact(99, "f2"),
        ]
        unit_ids = ["u1", "u2"]

        result = _map_results_to_contents(contents, processed, unit_ids)

        assert result == [["u1"], ["u2"]]

    def test_empty_inputs(self):
        assert _map_results_to_contents([], [], []) == []

    def test_length_mismatch_raises(self):
        # Regression for #1037: previously the function silently overran unit_ids.
        contents = [_make_content("a")]
        processed = [_make_processed_fact(0), _make_processed_fact(0)]
        unit_ids = ["u1"]  # one fewer than processed_facts

        with pytest.raises(ValueError, match="length mismatch"):
            _map_results_to_contents(contents, processed, unit_ids)

    def test_unit_ids_assigned_by_processed_fact_position(self):
        # Even if processed_facts are interleaved across contents, each unit_id
        # must follow its corresponding processed_fact (positional alignment).
        contents = [_make_content("a"), _make_content("b")]
        processed = [
            _make_processed_fact(1, "b1"),
            _make_processed_fact(0, "a1"),
            _make_processed_fact(1, "b2"),
        ]
        unit_ids = ["u-b1", "u-a1", "u-b2"]

        result = _map_results_to_contents(contents, processed, unit_ids)

        assert result == [["u-a1"], ["u-b1", "u-b2"]]


class TestProcessExtractedFacts:
    def test_filters_extracted_and_processed_facts_in_lockstep(self):
        extracted = [
            _make_extracted_fact("Alice joined Acme", 10),
            _make_extracted_fact("...", 11),
            _make_extracted_fact("Bob leads the ML team", 12),
        ]
        embeddings = [[10.0], [11.0], [12.0]]

        result = _process_extracted_facts(extracted, embeddings)

        assert [fact.fact_text for fact in result.extracted_facts] == ["Alice joined Acme", "Bob leads the ML team"]
        assert [fact.chunk_index for fact in result.extracted_facts] == [10, 12]
        assert [fact.fact_text for fact in result.processed_facts] == ["Alice joined Acme", "Bob leads the ML team"]
        assert [fact.embedding for fact in result.processed_facts] == [[10.0], [12.0]]
        assert result.retained_index_by_original == [0, None, 1]

    def test_remaps_canonical_relations_and_drops_rejected_targets(self):
        extracted = [
            _make_extracted_fact("Initial event", 10),
            _make_extracted_fact("...", 11),
            _make_extracted_fact("Consequence", 12, [0, 1]),
            _make_extracted_fact("Later consequence", 13, [1, 2]),
        ]

        result = _process_extracted_facts(extracted, [[0.0], [1.0], [2.0], [3.0]])

        assert [
            [relation.target_fact_index for relation in fact.causal_relations] for fact in result.processed_facts
        ] == [
            [],
            [0],
            [1],
        ]
        assert [fact.causal_relations for fact in result.extracted_facts] == [
            fact.causal_relations for fact in result.processed_facts
        ]

    def test_remaps_transfer_relation_matrix_with_original_source_ordinals(self):
        relations = [
            [],
            [],
            [
                CausalRelation(relation_type="causes", target_fact_index=0),
                CausalRelation(relation_type="prevents", target_fact_index=1),
            ],
            [CausalRelation(relation_type="enables", target_fact_index=2)],
        ]

        remapped = _remap_causal_relations(relations, [0, None, 1, 2])

        assert [
            [(relation.relation_type, relation.target_fact_index) for relation in fact_relations]
            for fact_relations in remapped
        ] == [[], [("causes", 0)], [("enables", 1)]]

    def test_rejects_fact_embedding_length_mismatch(self):
        extracted = [_make_extracted_fact("one", 1), _make_extracted_fact("two", 2)]

        with pytest.raises(ValueError, match="length mismatch"):
            _process_extracted_facts(extracted, [[1.0]])


class TestEmbeddingsBatchLengthGuarantee:
    def test_raises_when_backend_returns_fewer_embeddings(self):
        # Regression for #1037: backends that silently truncate must not pass
        # through — `zip(extracted_facts, embeddings)` would otherwise drop
        # facts and break unit_id alignment downstream.
        backend = MagicMock()
        backend.encode_documents.return_value = [[0.1, 0.2]]  # only 1 vector for 3 inputs

        with pytest.raises(RuntimeError, match="returned 1 vectors for 3 input texts"):
            asyncio.run(embedding_utils.generate_embeddings_batch(backend, ["a", "b", "c"]))

    def test_raises_when_backend_returns_more_embeddings(self):
        backend = MagicMock()
        backend.encode_documents.return_value = [[0.1], [0.2], [0.3]]

        with pytest.raises(RuntimeError, match="returned 3 vectors for 2 input texts"):
            asyncio.run(embedding_utils.generate_embeddings_batch(backend, ["a", "b"]))

    def test_passes_through_aligned_embeddings(self):
        backend = MagicMock()
        backend.dimension = 1
        backend.encode_documents.return_value = [[0.1], [0.2]]

        result = asyncio.run(embedding_utils.generate_embeddings_batch(backend, ["a", "b"]))

        assert result == [[0.1], [0.2]]

    def test_raises_when_backend_returns_empty_embedding_vector(self):
        backend = MagicMock()
        backend.dimension = 3
        backend.encode_documents.return_value = [[0.1, 0.2, 0.3], []]

        with pytest.raises(RuntimeError, match="embedding 1 has dimension 0; expected 3"):
            asyncio.run(embedding_utils.generate_embeddings_batch(backend, ["a", "b"]))

    def test_raises_when_backend_returns_wrong_embedding_dimension(self):
        backend = MagicMock()
        backend.dimension = 3
        backend.encode_documents.return_value = [[0.1, 0.2, 0.3], [0.4, 0.5]]

        with pytest.raises(RuntimeError, match="embedding 1 has dimension 2; expected 3"):
            asyncio.run(embedding_utils.generate_embeddings_batch(backend, ["a", "b"]))


class TestSemanticLinkThresholdPropagation:
    @pytest.mark.asyncio
    async def test_phase1_ann_uses_resolved_semantic_link_threshold(self, monkeypatch):
        """Normal retain must pass the resolved threshold into its pre-write ANN probe."""
        captured_thresholds: list[float] = []

        @asynccontextmanager
        async def fake_acquire_with_retry(_pool):
            yield object()

        async def fake_resolve_entities(*_args, **_kwargs):
            return EntityResolutionResult(resolved_entities=[], entity_to_unit=[], unit_to_entity_ids={})

        async def fake_compute_semantic_links_ann(*_args, **kwargs):
            captured_thresholds.append(kwargs["threshold"])
            return []

        monkeypatch.setattr(orchestrator, "acquire_with_retry", fake_acquire_with_retry)
        monkeypatch.setattr(entity_processing, "resolve_entities", fake_resolve_entities)
        monkeypatch.setattr(link_utils, "compute_semantic_links_ann", fake_compute_semantic_links_ann)

        await _pre_resolve_phase1(
            pool=object(),
            entity_resolver=object(),
            bank_id="bank",
            contents=[_make_content()],
            processed_facts=[_make_processed_fact(0)],
            config=SimpleNamespace(entity_labels=None, semantic_link_min_similarity=0.82),
            log_buffer=[],
        )

        assert captured_thresholds == [0.82]

    @pytest.mark.asyncio
    async def test_link_creation_forwards_threshold_to_link_utils(self, monkeypatch):
        """The Phase 2 wrapper forwards the resolved threshold to link_utils."""
        captured_thresholds: list[float] = []

        async def fake_create_semantic_links_batch(*_args, **kwargs):
            captured_thresholds.append(kwargs["threshold"])
            return 0

        monkeypatch.setattr(link_utils, "create_semantic_links_batch", fake_create_semantic_links_batch)

        await link_creation.create_semantic_links_batch(
            conn=object(),
            bank_id="bank",
            unit_ids=["unit"],
            embeddings=[[1.0]],
            threshold=0.83,
        )

        assert captured_thresholds == [0.83]

    @pytest.mark.asyncio
    async def test_streaming_final_ann_uses_resolved_threshold(self, monkeypatch):
        """Streaming retain's deferred ANN pass uses the same configured construction gate."""
        captured_thresholds: list[float] = []
        conn = SimpleNamespace(
            fetch=AsyncMock(return_value=[{"id": "unit", "embedding": "[1.0]", "fact_type": "world"}])
        )
        pool = SimpleNamespace(ops=object())

        @asynccontextmanager
        async def fake_acquire_with_retry(_pool):
            yield conn

        async def fake_compute_semantic_links_ann(*_args, **kwargs):
            captured_thresholds.append(kwargs["threshold"])
            return []

        monkeypatch.setattr(orchestrator, "acquire_with_retry", fake_acquire_with_retry)
        monkeypatch.setattr(link_utils, "compute_semantic_links_ann", fake_compute_semantic_links_ann)

        await _run_final_semantic_ann(
            pool,
            "bank",
            ["unit"],
            threshold=0.84,
            log_buffer=[],
        )

        assert captured_thresholds == [0.84]


class _SelfIndexedStore(PostgresMemories):
    """A store that owns its vector index, so it derives the semantic neighbours itself.

    Subclasses the Postgres store because only the capability answer matters here — the gates
    ask this one question and nothing else on the store is reached.
    """

    name = "self-indexed"
    derives_semantic_links_internally = True

    def __init__(self, sql_link_banks: set[str] | None = None):
        super().__init__({})
        # Banks this store still leaves to the Postgres link graph (empty = it owns them all).
        self._sql_link_banks = sql_link_banks or set()

    def derives_semantic_links_internally_for(self, bank_id: str) -> bool:
        return bank_id not in self._sql_link_banks


class TestSelfIndexedStoreSkipsSemanticLinkWork:
    """A store that derives the semantic graph itself must see NO retain-time kNN work in
    Postgres — not the ANN probe, and not the ``memory_links`` rows the probe feeds. Both halves
    are gated at their single choke point so every retain path (full, delta, import) inherits the
    skip; these tests pin each gate, since a store that opts in has no ``memory_links`` for the
    SQL graph arm to fall back on."""

    @pytest.mark.asyncio
    async def test_phase1_skips_the_ann_probe(self, monkeypatch):
        """Phase 1 is where delta retain and bank import run their ANN probe."""
        ann_calls: list[str] = []

        @asynccontextmanager
        async def fake_acquire_with_retry(_pool):
            yield object()

        async def fake_resolve_entities(*_args, **_kwargs):
            return EntityResolutionResult(resolved_entities=[], entity_to_unit=[], unit_to_entity_ids={})

        async def fake_compute_semantic_links_ann(_conn, bank_id, *_args, **_kwargs):
            ann_calls.append(bank_id)
            return [("unit", "other", "semantic", 0.9, None)]

        # Owns the index for every bank except "legacy-sql-bank" — the answer is per bank.
        store = _SelfIndexedStore({"legacy-sql-bank"})
        monkeypatch.setattr(orchestrator, "acquire_with_retry", fake_acquire_with_retry)
        monkeypatch.setattr(entity_processing, "resolve_entities", fake_resolve_entities)
        monkeypatch.setattr(link_utils, "compute_semantic_links_ann", fake_compute_semantic_links_ann)
        monkeypatch.setattr(memories_mod, "get_memories", lambda: store)

        async def run(bank_id: str):
            return await _pre_resolve_phase1(
                pool=object(),
                entity_resolver=object(),
                bank_id=bank_id,
                contents=[_make_content()],
                processed_facts=[_make_processed_fact(0)],
                config=SimpleNamespace(entity_labels=None, semantic_link_min_similarity=0.82),
                log_buffer=[],
            )

        owned = await run("vector-bank")
        assert ann_calls == []
        assert owned.semantic_ann_links == []

        # A bank this store leaves in SQL still gets the probe, on the same store instance.
        legacy = await run("legacy-sql-bank")
        assert ann_calls == ["legacy-sql-bank"]
        assert len(legacy.semantic_ann_links) == 1

    @pytest.mark.asyncio
    async def test_phase2_writes_no_semantic_memory_links(self, monkeypatch):
        """The within-batch similarities are computed in Python, so skipping the ANN probe alone
        would still leave ``memory_links`` rows behind. Phase 2 has to skip the write too."""
        semantic_writes: list[str] = []

        async def fake_insert_facts_batch(*_args, **_kwargs):
            return ["unit-1"]

        async def fake_create_semantic_links_batch(_conn, bank_id, *_args, **_kwargs):
            semantic_writes.append(bank_id)
            return 1

        async def fake_create_temporal_links_batch(*_args, **_kwargs):
            return 0

        async def fake_create_causal_links_batch(*_args, **_kwargs):
            return 0

        store = _SelfIndexedStore({"legacy-sql-bank"})
        monkeypatch.setattr(fact_storage, "insert_facts_batch", fake_insert_facts_batch)
        monkeypatch.setattr(link_creation, "create_semantic_links_batch", fake_create_semantic_links_batch)
        monkeypatch.setattr(link_creation, "create_temporal_links_batch", fake_create_temporal_links_batch)
        monkeypatch.setattr(link_creation, "create_causal_links_batch", fake_create_causal_links_batch)
        monkeypatch.setattr(memories_mod, "get_memories", lambda: store)

        entity_resolver = SimpleNamespace(
            reassert_entities_batch=AsyncMock(),
            link_units_to_entities_batch=AsyncMock(),
        )

        async def run(bank_id: str):
            return await _insert_facts_and_links(
                object(),
                entity_resolver,
                bank_id,
                [_make_content()],
                [_make_extracted_fact("f", 0)],
                [_make_processed_fact(0)],
                SimpleNamespace(semantic_link_min_similarity=0.82),
                [],
                resolved_entities=[],
                entity_to_unit=[],
                unit_to_entity_ids={},
                semantic_ann_links=[],
            )

        assert await run("vector-bank") == [["unit-1"]]
        assert semantic_writes == []

        # ...and the store's SQL-backed bank keeps the Postgres link graph.
        assert await run("legacy-sql-bank") == [["unit-1"]]
        assert semantic_writes == ["legacy-sql-bank"]

    @pytest.mark.asyncio
    async def test_streaming_final_ann_pass_is_skipped(self, monkeypatch):
        """Streaming (full) retain defers its links to this post-commit pass, which reaches
        pgvector directly rather than through Phase 1 — so it carries its own guard. Nothing
        should be loaded from memory_units either: the guard runs before the lookup."""
        conn = SimpleNamespace(fetch=AsyncMock(return_value=[]))
        ann_calls: list[str] = []

        @asynccontextmanager
        async def fake_acquire_with_retry(_pool):
            yield conn

        async def fake_compute_semantic_links_ann(*_args, **_kwargs):
            ann_calls.append("called")
            return []

        monkeypatch.setattr(orchestrator, "acquire_with_retry", fake_acquire_with_retry)
        monkeypatch.setattr(link_utils, "compute_semantic_links_ann", fake_compute_semantic_links_ann)
        monkeypatch.setattr(memories_mod, "get_memories", lambda: _SelfIndexedStore())

        await _run_final_semantic_ann(
            SimpleNamespace(ops=object()),
            "vector-bank",
            ["unit"],
            threshold=0.84,
            log_buffer=[],
        )

        assert ann_calls == []
        conn.fetch.assert_not_awaited()
