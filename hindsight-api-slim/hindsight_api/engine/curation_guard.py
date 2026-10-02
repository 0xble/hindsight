"""Opt-in, source-bound preconditions for a raw-fact curation write.

The client owns batch receipts and its cooperative lease. The server owns the
atomic check: no dependent observation may be deleted by a guarded write.
"""

import hashlib
import json
import uuid
from collections.abc import Callable, Mapping
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .db import DatabaseConnection


class CurationConflictError(ValueError):
    """The reviewed snapshot is no longer safe to curate."""


class CurationGuard(BaseModel):
    model_config = ConfigDict(extra="forbid")

    protocol: Literal["raw-curation-v1"]
    expected_memory_sha256: str = Field(min_length=64, max_length=64, description="Lowercase SHA-256 hex digest.")
    expected_source_sha256: str = Field(min_length=64, max_length=64, description="Lowercase SHA-256 hex digest.")
    require_no_observations: Literal[True]
    require_quiescent_consolidation: Literal[True]

    @field_validator("expected_memory_sha256", "expected_source_sha256")
    @classmethod
    def validate_digest(cls, value: str) -> str:
        # Keep pattern validation server-side. Exposing a JSON Schema pattern
        # makes progenitor generate a regress-crate dependency which the
        # published Rust client does not carry. The length remains advertised.
        if any(character not in "0123456789abcdef" for character in value):
            raise ValueError("Expected a lowercase SHA-256 hex digest.")
        return value


class CurationMemorySnapshot(BaseModel):
    """Fixed projection of the raw-memory GET response, without attachments."""

    id: str
    text: str
    context: str
    date: str
    type: Literal["world", "experience"]
    mentioned_at: str | None
    occurred_start: str | None
    occurred_end: str | None
    entities: list[str]
    document_id: str | None
    chunk_id: str | None
    tags: list[str]
    metadata: dict[str, Any]
    observation_scopes: Any
    state: Literal["valid", "invalidated"]
    invalidation_reason: str | None
    invalidated_at: str | None
    edited_at: str | None


class CurationSourceSnapshot(BaseModel):
    document_id: str
    content_hash: str | None
    updated_at: str | None
    original_text_sha256: str
    chunk_id: str
    chunk_text_sha256: str


def snapshot_sha256(snapshot: BaseModel) -> str:
    encoded = json.dumps(
        snapshot.model_dump(), ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def memory_snapshot_sha256(memory: Mapping[str, Any]) -> str:
    snapshot = CurationMemorySnapshot.model_validate(memory)
    snapshot.entities = sorted(snapshot.entities)
    snapshot.tags = sorted(snapshot.tags)
    return snapshot_sha256(snapshot)


async def lock_curation_tables(conn: DatabaseConnection, fq_table: Callable[[str], str]) -> None:
    """Bound the PostgreSQL-only write window, including observation phantoms.

    Source-ID arrays have no FK locking discipline. A row lock on the raw fact
    alone cannot prevent a concurrent observation insert between check and
    cascade. Opt-in guards therefore use short table locks, with a fixed order
    and fail-fast NOWAIT. Ordinary unguarded curation keeps its existing path.
    """
    if conn.backend_type != "postgresql":
        raise CurationConflictError("Guarded curation requires the PostgreSQL memory store.")
    # SELECT ... FOR UPDATE takes a compatible ROW SHARE table lock: NOWAIT
    # table locks alone cannot stop a later DML statement waiting on its rows.
    # Bound every lock acquisition until commit, without leaking to pool reuse.
    await conn.execute("SET LOCAL lock_timeout = '100ms'")
    for table in (
        "async_operations",
        "banks",
        "chunks",
        "documents",
        "entities",
        "invalidated_memory_units",
        "memory_units",
        "unit_entities",
    ):
        # SHARE ROW EXCLUSIVE permits this transaction's curation while blocking
        # competing DML. NOWAIT avoids pinning a pool connection behind a worker.
        await conn.execute(f"LOCK TABLE {fq_table(table)} IN SHARE ROW EXCLUSIVE MODE NOWAIT")


async def verify_curation_guard(
    *,
    conn: DatabaseConnection,
    fq_table: Callable[[str], str],
    bank_id: str,
    memory: Mapping[str, Any] | None,
    guard: CurationGuard,
) -> None:
    """Check while the caller holds the locks through the curation commit."""
    if memory is None or memory_snapshot_sha256(memory) != guard.expected_memory_sha256:
        raise CurationConflictError("Memory snapshot changed or is unavailable.")
    snapshot = CurationMemorySnapshot.model_validate(memory)
    if not snapshot.document_id or not snapshot.chunk_id:
        raise CurationConflictError("Guarded curation requires a source document and chunk.")
    # Require an explicit paused override, rather than a potentially cached or
    # inherited configuration. Only the consolidation owner changes this flag.
    bank = await conn.fetchrow(f"SELECT config FROM {fq_table('banks')} WHERE bank_id = $1", bank_id)
    config = conn.parse_json(bank["config"]) if bank else None
    if not isinstance(config, dict) or config.get("enable_auto_consolidation") is not False:
        raise CurationConflictError("Automatic consolidation must be explicitly paused.")
    active = await conn.fetchval(
        f"SELECT EXISTS (SELECT 1 FROM {fq_table('async_operations')} WHERE bank_id = $1 "
        "AND operation_type = 'consolidation' AND status IN ('pending', 'processing'))",
        bank_id,
    )
    if active:
        raise CurationConflictError("Consolidation has an active operation.")
    dependent = await conn.fetchval(
        f"SELECT EXISTS (SELECT 1 FROM {fq_table('memory_units')} WHERE bank_id = $1 "
        "AND fact_type = 'observation' AND source_memory_ids && $2::uuid[])",
        bank_id,
        [uuid.UUID(snapshot.id)],
    )
    if dependent:
        raise CurationConflictError("Memory has dependent observations, curation would delete derived state.")
    source = await conn.fetchrow(
        f"SELECT d.content_hash, d.updated_at, d.original_text, c.chunk_text "
        f"FROM {fq_table('documents')} d JOIN {fq_table('chunks')} c "
        "ON c.document_id = d.id AND c.bank_id = d.bank_id "
        "WHERE d.id = $1 AND d.bank_id = $2 AND c.chunk_id = $3 "
        "AND octet_length(d.original_text) <= 1048576 AND octet_length(c.chunk_text) <= 1048576",
        snapshot.document_id,
        bank_id,
        snapshot.chunk_id,
    )
    if source is None or source["original_text"] is None or source["chunk_text"] is None:
        raise CurationConflictError("Source text is unavailable or exceeds the guarded 1 MiB per-body limit.")
    source_snapshot = CurationSourceSnapshot(
        document_id=snapshot.document_id,
        content_hash=source["content_hash"],
        updated_at=source["updated_at"].isoformat() if source["updated_at"] else None,
        original_text_sha256=hashlib.sha256(source["original_text"].encode("utf-8")).hexdigest(),
        chunk_id=snapshot.chunk_id,
        chunk_text_sha256=hashlib.sha256(source["chunk_text"].encode("utf-8")).hexdigest(),
    )
    if snapshot_sha256(source_snapshot) != guard.expected_source_sha256:
        raise CurationConflictError("Source snapshot changed.")
