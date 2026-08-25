"""``chunks`` extraction mode gets its own sub-batch budget (issue #3784).

``retain_batch_tokens`` is sized to keep a fact-extraction prompt inside a context
window. ``chunks`` mode never builds that prompt — it stores each chunk as-is — so
applying the same ceiling there only capped how many chunks the streaming pipeline
could hold at once (~13-29 at the default chunk size), which serialised the per-chunk
fan-out and left ``retain_chunk_batch_size`` permanently unreachable.

These tests cover the two mechanical halves: picking the budget, and what the splitter
then does with a document under it. Both are deterministic — no LLM involved.
"""

import pytest

from hindsight_api.config import (
    DEFAULT_RETAIN_BATCH_TOKENS,
    DEFAULT_RETAIN_CHUNK_BATCH_SIZE,
    DEFAULT_RETAIN_CHUNK_SIZE,
    DEFAULT_RETAIN_CHUNKS_MODE_BATCH_TOKENS,
)
from hindsight_api.engine.memory_engine import RetainContentDict, count_tokens, sub_batch_token_budget
from tests.sub_batch_helpers import collect_sub_batches


@pytest.mark.parametrize("mode", ["concise", "verbose", "custom", "verbatim"])
def test_llm_modes_keep_the_context_sized_budget(mode: str) -> None:
    """Every mode that builds an extraction prompt keeps the context-shaped ceiling."""
    assert sub_batch_token_budget(mode, default_tokens=10_000, chunks_mode_tokens=500_000) == 10_000


def test_chunks_mode_uses_the_memory_sized_budget() -> None:
    assert sub_batch_token_budget("chunks", default_tokens=10_000, chunks_mode_tokens=500_000) == 500_000


def test_shipped_defaults_let_chunks_mode_hold_more_than_the_llm_modes() -> None:
    """Guard the relationship, not the numbers: the chunks budget must be the larger one.

    Equal or smaller defaults would silently restore the serialised behaviour this
    exists to fix, and nothing else in the pipeline would notice.
    """
    assert DEFAULT_RETAIN_CHUNKS_MODE_BATCH_TOKENS > DEFAULT_RETAIN_BATCH_TOKENS


def _prose(chars: int) -> str:
    """Plain text with paragraph breaks, so the chunker splits it predictably."""
    para = ("The memory engine retains a document by chunking it first. " * 12).strip()
    out: list[str] = []
    size = 0
    while size < chars:
        out.append(para)
        size += len(para) + 2
    return "\n\n".join(out)


def test_chunks_mode_budget_reaches_the_streaming_batch_size() -> None:
    """A document is handed over in slices big enough for retain_chunk_batch_size to bind.

    Under the LLM budget every slice is smaller than ``retain_chunk_batch_size``, so the
    streaming pipeline's own batching never applies — the slice boundary is always what
    ends a batch. That is the defect: the knob that is supposed to size a streaming batch
    is dominated by one that has nothing to do with this mode.
    """
    body = _prose(600_000)
    contents: list[RetainContentDict] = [{"content": body, "document_id": "doc-1"}]
    assert count_tokens(body) > DEFAULT_RETAIN_BATCH_TOKENS

    under_llm_budget = collect_sub_batches(contents, DEFAULT_RETAIN_BATCH_TOKENS, chunk_size=DEFAULT_RETAIN_CHUNK_SIZE)
    under_chunks_budget = collect_sub_batches(
        contents, DEFAULT_RETAIN_CHUNKS_MODE_BATCH_TOKENS, chunk_size=DEFAULT_RETAIN_CHUNK_SIZE
    )

    # Same document, same chunk boundaries — only the packing differs.
    assert sum(under_llm_budget.chunk_counts) == sum(under_chunks_budget.chunk_counts)

    assert max(under_llm_budget.chunk_counts) < DEFAULT_RETAIN_CHUNK_BATCH_SIZE
    assert max(under_chunks_budget.chunk_counts) >= DEFAULT_RETAIN_CHUNK_BATCH_SIZE
    assert len(under_chunks_budget.sub_batches) < len(under_llm_budget.sub_batches)
