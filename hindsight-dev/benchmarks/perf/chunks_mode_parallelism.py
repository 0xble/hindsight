"""Measure retain parallelism in chunks extraction mode.

Ingests one synthetic document through the real retain pipeline with
``HINDSIGHT_API_LLM_PROVIDER=none`` (which forces ``retain_extraction_mode='chunks'``)
and reports what the pipeline actually did: how many sequential streaming passes ran,
how wide the embedding fan-out got, and end-to-end chunk throughput.

Usage:
    uv run python benchmarks/perf/chunks_mode_parallelism.py --mb 2 [--batch-tokens N]
"""

import argparse
import asyncio
import json
import logging
import os
import random
import statistics
import time
import tracemalloc
import uuid

WORDS = [
    "alpha",
    "beta",
    "gamma",
    "delta",
    "memory",
    "engine",
    "retain",
    "chunk",
    "document",
    "the",
    "a",
    "of",
    "system",
    "vector",
    "index",
    "embedding",
    "fact",
    "entity",
    "graph",
    "cluster",
    "latency",
    "throughput",
    "pipeline",
    "consolidation",
    "observation",
]


def make_prose(target_chars: int, seed: int = 7) -> str:
    rng = random.Random(seed)
    parts: list[str] = []
    size = 0
    while size < target_chars:
        para = []
        for _ in range(rng.randint(3, 12)):
            sent = " ".join(rng.choice(WORDS) for _ in range(rng.randint(5, 30))) + "."
            para.append(sent)
        p = " ".join(para)
        parts.append(p)
        size += len(p) + 2
    return "\n\n".join(parts)


class _StreamingLogCapture(logging.Handler):
    """Collect the retain pipeline's own log lines for post-hoc analysis."""

    def __init__(self) -> None:
        super().__init__(level=logging.INFO)
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = record.getMessage()
        except Exception:
            return
        if "[streaming]" in msg or "sub-batch" in msg or "Large batch detected" in msg:
            self.lines.extend(msg.splitlines())


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mb", type=float, default=2.0, help="Document size in MB")
    parser.add_argument("--batch-tokens", type=int, default=None, help="Override HINDSIGHT_API_RETAIN_BATCH_TOKENS")
    parser.add_argument(
        "--chunks-mode-batch-tokens",
        type=int,
        default=None,
        help="Override HINDSIGHT_API_RETAIN_CHUNKS_MODE_BATCH_TOKENS",
    )
    parser.add_argument(
        "--chunk-batch-size", type=int, default=None, help="Override HINDSIGHT_API_RETAIN_CHUNK_BATCH_SIZE"
    )
    parser.add_argument("--label", default="run")
    parser.add_argument("--output", default=None)
    args = parser.parse_args()

    if args.batch_tokens is not None:
        os.environ["HINDSIGHT_API_RETAIN_BATCH_TOKENS"] = str(args.batch_tokens)
    if args.chunks_mode_batch_tokens is not None:
        os.environ["HINDSIGHT_API_RETAIN_CHUNKS_MODE_BATCH_TOKENS"] = str(args.chunks_mode_batch_tokens)
    if args.chunk_batch_size is not None:
        os.environ["HINDSIGHT_API_RETAIN_CHUNK_BATCH_SIZE"] = str(args.chunk_batch_size)

    from hindsight_api import MemoryEngine
    from hindsight_api.config import _get_raw_config
    from hindsight_api.engine.retain import chunk_storage, embedding_utils
    from hindsight_api.models import RequestContext

    # Instrument the embedding backend call: every batch the coalescer actually sends.
    calls: list[tuple[float, float, int]] = []
    inflight = 0
    peak_inflight = 0
    original = embedding_utils.generate_embeddings_batch

    async def _instrumented(backend, texts, *a, **kw):
        nonlocal inflight, peak_inflight
        inflight += 1
        peak_inflight = max(peak_inflight, inflight)
        t0 = time.perf_counter()
        try:
            return await original(backend, texts, *a, **kw)
        finally:
            calls.append((t0, time.perf_counter(), len(texts)))
            inflight -= 1

    embedding_utils.generate_embeddings_batch = _instrumented

    # Instrument the DB consumer: one call per committed mini-batch, so we can see
    # whether retain_chunk_batch_size actually bounds anything.
    commits: list[int] = []
    original_store = chunk_storage.store_chunks_batch

    async def _instrumented_store(conn, bank_id, document_id, chunks, *a, **kw):
        commits.append(len(chunks))
        return await original_store(conn, bank_id, document_id, chunks, *a, **kw)

    chunk_storage.store_chunks_batch = _instrumented_store

    capture = _StreamingLogCapture()
    logging.getLogger().addHandler(capture)
    logging.getLogger().setLevel(logging.INFO)

    text = make_prose(int(args.mb * 1024 * 1024))
    cfg = _get_raw_config()
    print(
        f"[{args.label}] doc={len(text):,} chars  retain_batch_tokens={cfg.retain_batch_tokens:,}  "
        f"chunks_mode={cfg.retain_chunks_mode_batch_tokens:,}  "
        f"retain_chunk_batch_size={cfg.retain_chunk_batch_size}  chunk_size={cfg.retain_chunk_size}"
    )

    memory = MemoryEngine(db_url=os.getenv("HINDSIGHT_API_DATABASE_URL", "pg0"), memory_llm_provider="none")
    await memory.initialize()
    bank_id = f"chunkperf-{uuid.uuid4().hex[:8]}"
    ctx = RequestContext()

    tracemalloc.start()
    t0 = time.perf_counter()
    result = await memory.retain_batch_async(
        bank_id=bank_id,
        contents=[{"content": text, "document_id": "bench-doc"}],
        request_context=ctx,
    )
    wall = time.perf_counter() - t0
    _cur, peak_bytes = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    listed = await memory.list_document_chunks(bank_id=bank_id, document_id="bench-doc", limit=1, request_context=ctx)
    n_chunks = int(listed.get("total", 0))

    passes = sum(1 for line in capture.lines if "chunks, batch_size" in line)
    sizes = [n for _, _, n in calls]
    span = (max(e for _, e, _ in calls) - min(s for s, _, _ in calls)) if calls else 0.0
    busy = sum(e - s for s, e, _ in calls)

    # The two instrumentation points are monkeypatches onto module attributes. If either
    # function is renamed or a caller stops going through it, the patch stops firing and the
    # run would report a flattering zero instead of failing — so refuse to publish numbers
    # that were never measured.
    if not calls:
        raise RuntimeError(
            "no embedding batches recorded — embedding_utils.generate_embeddings_batch is no "
            "longer the path the retain producer embeds through; re-point the instrumentation"
        )
    if not commits:
        raise RuntimeError(
            "no chunk commits recorded — chunk_storage.store_chunks_batch is no longer the "
            "path the streaming consumer writes through; re-point the instrumentation"
        )

    report = {
        "label": args.label,
        "doc_chars": len(text),
        "retain_batch_tokens": cfg.retain_batch_tokens,
        "retain_chunks_mode_batch_tokens": cfg.retain_chunks_mode_batch_tokens,
        "retain_chunk_batch_size": cfg.retain_chunk_batch_size,
        "wall_seconds": round(wall, 2),
        "chunks_stored": n_chunks,
        "chunks_per_second": round(n_chunks / wall, 2) if wall else 0,
        "streaming_passes": passes,
        "embed_requests": len(calls),
        "embed_texts": sum(sizes),
        "embed_batch_min": min(sizes) if sizes else 0,
        "embed_batch_median": statistics.median(sizes) if sizes else 0,
        "embed_batch_max": max(sizes) if sizes else 0,
        "embed_peak_inflight": peak_inflight,
        "embed_avg_concurrency": round(busy / span, 2) if span else 0,
        "retain_peak_mb": round(peak_bytes / 1e6, 1),
        "db_commits": len(commits),
        "db_commit_min": min(commits) if commits else 0,
        "db_commit_median": statistics.median(commits) if commits else 0,
        "db_commit_max": max(commits) if commits else 0,
        "units": sum(len(r) for r in result) if result else 0,
    }
    print(json.dumps(report, indent=2))
    if args.output:
        with open(args.output, "w") as f:
            json.dump(report, f, indent=2)

    await memory.delete_bank(bank_id=bank_id, request_context=ctx)
    await memory.close()


if __name__ == "__main__":
    asyncio.run(main())
