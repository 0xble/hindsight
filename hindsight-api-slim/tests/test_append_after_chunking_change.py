"""
An append must not re-extract a document just because its chunk boundaries moved.

Delta retain matches stored chunks to freshly computed ones by hash at equal index. When the
effective chunk size for a document changes between retains (a bank edits ``retain_chunk_size``,
or the caller starts naming a strategy that sets a different one), none of the re-chunked pieces
are byte-identical to what is stored, so the diff finds zero unchanged chunks and falls back to a
full retain — re-extracting the whole history to add one turn.

For an APPEND that is avoidable: the new body is the stored body plus a tail, so the stored chunks
still describe the stored prefix exactly. Only the tail needs extraction.
"""

import json
from datetime import datetime, timezone

import pytest

from hindsight_api.config import clear_config_cache
from hindsight_api.engine.retain import fact_extraction

# Each turn is ~1.1 KB, so the default 3000-char chunker packs 2 turns per chunk while a
# 12000-char strategy packs ~10. That makes every stored boundary disagree with the re-chunk.
_TURN_FILLER = "lorem ipsum dolor sit amet " * 40
_BASE_TURNS = 12


def _ts() -> float:
    return datetime.now(timezone.utc).timestamp()


def _turns(start: int, count: int) -> list[dict]:
    return [
        {"role": "user" if i % 2 == 0 else "assistant", "content": f"TURNMARK{i:03d} {_TURN_FILLER}"}
        for i in range(start, start + count)
    ]


class _ExtractionSpy:
    def __init__(self) -> None:
        self.texts: list[str] = []

    def install(self, monkeypatch) -> None:
        original = fact_extraction.extract_facts_from_contents

        async def _spy(contents, *args, **kwargs):
            self.texts.extend(c.content for c in contents)
            return await original(contents, *args, **kwargs)

        monkeypatch.setattr(fact_extraction, "extract_facts_from_contents", _spy)

    def seen(self, marker: str) -> bool:
        return any(marker in t for t in self.texts)


@pytest.fixture(autouse=True)
def _fast_retain_env(monkeypatch):
    monkeypatch.setenv("HINDSIGHT_API_ENABLE_AUTO_CONSOLIDATION", "false")
    monkeypatch.setenv("HINDSIGHT_API_ENABLE_OBSERVATIONS", "false")
    clear_config_cache()
    yield
    clear_config_cache()


async def _stored_chunk_count(memory, bank_id: str, document_id: str) -> int:
    async with memory._pool.acquire() as conn:
        return await conn.fetchval(
            "SELECT count(*) FROM chunks WHERE bank_id = $1 AND document_id = $2", bank_id, document_id
        )


async def _unit_count(memory, bank_id: str, document_id: str) -> int:
    async with memory._pool.acquire() as conn:
        return await conn.fetchval(
            "SELECT count(*) FROM memory_units WHERE bank_id = $1 AND document_id = $2", bank_id, document_id
        )


@pytest.mark.asyncio
@pytest.mark.memory_backend_incompatible
async def test_append_after_chunk_size_change_extracts_only_the_tail(memory, request_context, monkeypatch):
    bank_id = f"test_append_rechunk_{_ts()}"
    document_id = "conversation-rechunk"
    try:
        # History written under the bank default chunk size (3000).
        await memory.retain_batch_async(
            bank_id=bank_id,
            contents=[
                {
                    "content": json.dumps(_turns(0, _BASE_TURNS)),
                    "context": "conversation",
                    "document_id": document_id,
                    "update_mode": "append",
                }
            ],
            request_context=request_context,
        )
        chunks_before = await _stored_chunk_count(memory, bank_id, document_id)
        units_before = await _unit_count(memory, bank_id, document_id)
        assert chunks_before >= 4, f"fixture must span several default-size chunks, got {chunks_before}"
        assert units_before > 0

        # The caller now names a strategy with a larger chunk size, as a client does after
        # adopting per-source strategies. Nothing about the stored history changed.
        await memory.update_bank_config(
            bank_id,
            {"retain_strategies": {"session": {"retain_chunk_size": 12000}}},
            request_context=request_context,
        )
        clear_config_cache()

        spy = _ExtractionSpy()
        spy.install(monkeypatch)
        # The HTTP layer groups items by strategy and hands each group to the engine with
        # ``strategy=`` set, which is what resolves the strategy's chunk size.
        await memory.retain_batch_async(
            bank_id=bank_id,
            contents=[
                {
                    "content": json.dumps(_turns(_BASE_TURNS, 1)),
                    "context": "conversation",
                    "document_id": document_id,
                    "update_mode": "append",
                    "strategy": "session",
                }
            ],
            strategy="session",
            request_context=request_context,
        )

        tail_marker = f"TURNMARK{_BASE_TURNS:03d}"
        assert spy.seen(tail_marker), "the appended turn must be extracted"
        history_reextracted = [f"TURNMARK{i:03d}" for i in range(_BASE_TURNS) if spy.seen(f"TURNMARK{i:03d}")]
        assert not history_reextracted, (
            f"append re-extracted {len(history_reextracted)} of {_BASE_TURNS} unchanged history turns"
        )

        # History facts survive, the document holds every turn, and the chunk set still covers it.
        units_after = await _unit_count(memory, bank_id, document_id)
        assert units_after > units_before, "the tail's facts must be added alongside the history's"
        doc = await memory.get_document(document_id, bank_id, request_context=request_context)
        body = json.loads(doc["original_text"])
        assert [t["content"].split()[0] for t in body] == [f"TURNMARK{i:03d}" for i in range(_BASE_TURNS + 1)]
        async with memory._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT chunk_index, chunk_text FROM chunks WHERE bank_id = $1 AND document_id = $2 "
                "ORDER BY chunk_index",
                bank_id,
                document_id,
            )
        assert [r["chunk_index"] for r in rows] == list(range(len(rows))), "chunk indices must stay contiguous"
        covered = [e["content"].split()[0] for r in rows for e in json.loads(r["chunk_text"])]
        assert covered == [f"TURNMARK{i:03d}" for i in range(_BASE_TURNS + 1)], "chunks must cover the body in order"
    finally:
        await memory.delete_bank(bank_id, request_context=request_context)


@pytest.mark.asyncio
@pytest.mark.memory_backend_incompatible
async def test_append_after_rechunk_keeps_history_units_and_a_second_append_deltas(
    memory, request_context, monkeypatch
):
    """History facts stay the same rows (no re-extraction churn), and the next append at the new
    size still diffs cleanly against the mixed-size chunk set this leaves behind."""
    bank_id = f"test_append_rechunk2_{_ts()}"
    document_id = "conversation-rechunk-2"
    try:
        await memory.retain_batch_async(
            bank_id=bank_id,
            contents=[
                {"content": json.dumps(_turns(0, _BASE_TURNS)), "document_id": document_id, "update_mode": "append"}
            ],
            request_context=request_context,
        )
        async with memory._pool.acquire() as conn:
            history_ids = {
                r["id"]
                for r in await conn.fetch(
                    "SELECT id FROM memory_units WHERE bank_id = $1 AND document_id = $2", bank_id, document_id
                )
            }
        await memory.update_bank_config(
            bank_id, {"retain_strategies": {"session": {"retain_chunk_size": 12000}}}, request_context=request_context
        )
        clear_config_cache()
        for start in (_BASE_TURNS, _BASE_TURNS + 1):
            spy = _ExtractionSpy()
            spy.install(monkeypatch)
            await memory.retain_batch_async(
                bank_id=bank_id,
                contents=[
                    {
                        "content": json.dumps(_turns(start, 1)),
                        "document_id": document_id,
                        "update_mode": "append",
                        "strategy": "session",
                    }
                ],
                strategy="session",
                request_context=request_context,
            )
            reextracted = [i for i in range(start) if spy.seen(f"TURNMARK{i:03d}")]
            assert spy.seen(f"TURNMARK{start:03d}")
            assert not reextracted, f"append of turn {start} re-extracted earlier turns {reextracted}"
            monkeypatch.undo()
            monkeypatch.setenv("HINDSIGHT_API_ENABLE_AUTO_CONSOLIDATION", "false")
            monkeypatch.setenv("HINDSIGHT_API_ENABLE_OBSERVATIONS", "false")
            clear_config_cache()
        async with memory._pool.acquire() as conn:
            after_ids = {
                r["id"]
                for r in await conn.fetch(
                    "SELECT id FROM memory_units WHERE bank_id = $1 AND document_id = $2", bank_id, document_id
                )
            }
        assert history_ids <= after_ids, "history memory units must survive untouched"
    finally:
        await memory.delete_bank(bank_id, request_context=request_context)


def test_coverage_check_rejects_anything_but_an_exact_cover():
    from hindsight_api.engine.retain.orchestrator import _stored_chunks_cover_text

    turns = _turns(0, 4)
    body = json.dumps(turns)
    halves = [json.dumps(turns[:2]), json.dumps(turns[2:])]
    assert _stored_chunks_cover_text(halves, body)
    assert not _stored_chunks_cover_text(halves[:1], body), "missing turns"
    assert not _stored_chunks_cover_text(list(reversed(halves)), body), "out of order"
    assert not _stored_chunks_cover_text([json.dumps(turns[:2]), json.dumps(_turns(9, 2))], body), "edited turn"
    assert not _stored_chunks_cover_text([], body)

    text = "First sentence here. Second sentence there.\n\nThird paragraph."
    assert _stored_chunks_cover_text(["First sentence here.", "Second sentence there.", "Third paragraph."], text)
    assert not _stored_chunks_cover_text(["First sentence here.", "Third paragraph."], text), "gap"
    assert not _stored_chunks_cover_text(["First sentence here.", "Second sentence there."], text), "trailing"
