"""Per-bank recall pipeline stage toggles.

Recall runs four arms (semantic, BM25, graph, temporal) and then a cross-encoder
rerank. A bank whose content has no relational or temporal structure pays latency
for arms it cannot use, so each stage can be switched off per bank:

    enable_temporal_extraction / enable_graph_retrieval / enable_reranking

All three default to True, so recall behaviour is unchanged unless a bank opts out.
"""

from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from hindsight_api.engine import memory_engine as memory_engine_module
from hindsight_api.engine.search import retrieval as retrieval_module

_QUERY = "[0.1,0.2,0.3]"


@pytest.fixture
def stub_retrieval(monkeypatch):
    """Stub the DB-backed arms and record which ones actually ran."""
    calls = {"temporal_extraction": 0, "temporal_combined": 0, "graph": 0}

    @asynccontextmanager
    async def fake_acquire_with_retry(pool):
        yield object()

    async def fake_semantic_bm25_combined(*args, **kwargs):
        return {"world": retrieval_module.SemanticBm25Result(semantic=[], bm25=[], graph_seeds=None)}

    async def fake_temporal_combined(*args, **kwargs):
        calls["temporal_combined"] += 1
        return {"world": []}

    def fake_extract_temporal_constraint(*args, **kwargs):
        calls["temporal_extraction"] += 1
        from datetime import UTC, datetime

        return (datetime(2025, 1, 1, tzinfo=UTC), datetime(2025, 2, 1, tzinfo=UTC))

    class FakeGraphRetriever:
        async def retrieve(self, **kwargs):
            calls["graph"] += 1
            return [], None

    monkeypatch.setattr(retrieval_module, "acquire_with_retry", fake_acquire_with_retry)
    monkeypatch.setattr(retrieval_module, "retrieve_semantic_bm25_combined", fake_semantic_bm25_combined)
    monkeypatch.setattr(retrieval_module, "retrieve_temporal_combined", fake_temporal_combined)
    monkeypatch.setattr(
        "hindsight_api.engine.search.temporal_extraction.extract_temporal_constraint",
        fake_extract_temporal_constraint,
    )
    monkeypatch.setattr(
        retrieval_module,
        "get_config",
        lambda: SimpleNamespace(graph_seed_min_similarity=0.3, temporal_semantic_min_similarity=0.24),
    )
    return calls, FakeGraphRetriever


async def _run(graph_retriever, **kwargs):
    return await retrieval_module.retrieve_all_fact_types_parallel(
        object(),
        query_text="what happened in January?",
        query_embedding_str=_QUERY,
        bank_id="test_recall_pipeline_toggles",
        fact_types=["world"],
        thinking_budget=10,
        graph_retriever=graph_retriever,
        **kwargs,
    )


@pytest.mark.asyncio
async def test_all_stages_run_by_default(stub_retrieval):
    """Defaults preserve current behaviour: every arm runs."""
    calls, FakeGraphRetriever = stub_retrieval

    await _run(FakeGraphRetriever())

    assert calls["temporal_extraction"] == 1
    assert calls["temporal_combined"] == 1
    assert calls["graph"] == 1


@pytest.mark.asyncio
async def test_disabling_temporal_extraction_skips_the_temporal_arm(stub_retrieval):
    """No constraint extraction means nothing to filter on, so the temporal query is skipped too."""
    calls, FakeGraphRetriever = stub_retrieval

    result = await _run(FakeGraphRetriever(), enable_temporal_extraction=False)

    assert calls["temporal_extraction"] == 0
    assert calls["temporal_combined"] == 0
    # The other arms are unaffected.
    assert calls["graph"] == 1
    assert result.results_by_fact_type["world"].temporal is None


@pytest.mark.asyncio
async def test_disabling_graph_retrieval_skips_the_graph_arm(stub_retrieval):
    """The graph retriever is never invoked and the arm returns nothing."""
    calls, FakeGraphRetriever = stub_retrieval

    result = await _run(FakeGraphRetriever(), enable_graph_retrieval=False)

    assert calls["graph"] == 0
    # The other arms are unaffected.
    assert calls["temporal_extraction"] == 1
    assert calls["temporal_combined"] == 1
    assert result.results_by_fact_type["world"].graph == []


@pytest.mark.asyncio
async def test_disabling_graph_retrieval_does_not_resolve_a_retriever(stub_retrieval, monkeypatch):
    """With no retriever passed, the default is not constructed either — resolving one
    can lazily build a retriever, which is wasted work when the arm is off."""
    calls, _ = stub_retrieval

    def boom():
        raise AssertionError("default graph retriever must not be resolved when the arm is disabled")

    monkeypatch.setattr(retrieval_module, "get_default_graph_retriever", boom)

    await _run(None, enable_graph_retrieval=False)

    assert calls["graph"] == 0


@pytest.mark.parametrize(
    ("requested", "enable_reranking", "expected"),
    [
        # Default: unchanged.
        ("cross_encoder", True, "cross_encoder"),
        # Disabled: fall back to the RRF-fused ordering.
        ("cross_encoder", False, "rrf"),
        # Already rerank-free — nothing to downgrade.
        ("rrf", False, "rrf"),
        # An explicit caller choice (consolidation dedup) is never overridden: interleave
        # guarantees each arm a slot, which RRF does not, so clobbering it would change
        # which candidates consolidation sees.
        ("interleave", False, "interleave"),
        ("interleave", True, "interleave"),
    ],
)
def test_reranking_resolution(requested, enable_reranking, expected):
    resolved = memory_engine_module._resolve_reranking({"enable_reranking": enable_reranking}, requested)
    assert resolved == expected


def test_reranking_defaults_to_enabled_when_unset():
    """A bank with no override keeps the cross-encoder."""
    assert memory_engine_module._resolve_reranking({}, "cross_encoder") == "cross_encoder"
