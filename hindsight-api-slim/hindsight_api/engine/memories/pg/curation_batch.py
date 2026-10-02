"""Bounded PostgreSQL capsules. No extractor, provider or worker submission here."""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Iterable
from typing import Any
from uuid import UUID

import asyncpg
from pydantic import JsonValue, TypeAdapter

from ...curation_batch import (
    MAX_BYTES,
    MAX_ENTITIES,
    MAX_HISTORY,
    MAX_LINKS,
    MAX_OBSERVATIONS,
    MAX_PEERS,
    BatchCapsule,
    ClosureScope,
    ColumnSnapshot,
    CurationApplyRequest,
    CurationBatchConflict,
    CurationInventory,
    CurationPreview,
    CurationReceipt,
    CurationSnapshot,
    CurationTargetRevision,
    MaintenanceDebt,
    PreparedCorrection,
    SourceRevision,
    TableSnapshot,
    canonical_bytes,
    revision,
    snapshot_revision,
)
from ...db.base import DatabaseConnection
from ...schema import fq_table
from . import writes

_TABLES = (
    "banks",
    "documents",
    "chunks",
    "memory_units",
    "invalidated_memory_units",
    "observation_history",
    "memory_links",
    "entities",
    "unit_entities",
    "entity_cooccurrences",
    "graph_maintenance_queue",
    "entity_maintenance_queue",
    "async_operations",
    "curation_batches",
    "curation_entity_pins",
)


def _ids(values: Iterable[str | UUID]) -> list[str]:
    return sorted({str(v) for v in values})


async def lock(conn: DatabaseConnection) -> None:
    # NOWAIT prevents a fixed-order bulk lock request from deadlocking against
    # ordinary curation's row/table order. No work happens before all locks land.
    try:
        for table in _TABLES:
            await conn.execute(f"LOCK TABLE {fq_table(table)} IN SHARE ROW EXCLUSIVE MODE NOWAIT")
    except asyncpg.LockNotAvailableError as exc:
        raise CurationBatchConflict("Curation window is busy") from exc


async def assert_paused(conn: DatabaseConnection, bank_id: str) -> None:
    config = await conn.fetchval(f"SELECT config FROM {fq_table('banks')} WHERE bank_id=$1", bank_id)
    if isinstance(config, str):
        config = json.loads(config)
    if not config or config.get("enable_auto_consolidation") is not False:
        raise CurationBatchConflict("Bank consolidation must be explicitly paused")
    if await conn.fetchval(
        f"SELECT 1 FROM {fq_table('async_operations')} WHERE bank_id=$1 AND operation_type='consolidation' "
        "AND status IN ('pending','processing') LIMIT 1",
        bank_id,
    ):
        raise CurationBatchConflict("Consolidation is not quiescent")


async def table_snapshot(conn: DatabaseConnection, table: str, query: str, *args: Any, cap: int) -> TableSnapshot:
    columns = [
        ColumnSnapshot(name=r["name"], type=r["type"], generated=r["generated"])
        for r in await conn.fetch(
            "SELECT attname AS name,format_type(atttypid,atttypmod) AS type,attgenerated<>'' AS generated "
            "FROM pg_attribute WHERE attrelid=$1::regclass AND attnum>0 AND NOT attisdropped ORDER BY attnum",
            fq_table(table),
        )
    ]
    rows = await conn.fetch(query + f" LIMIT {cap + 1}", *args)
    if len(rows) > cap:
        raise CurationBatchConflict(f"Closure exceeds {table} row cap")
    parsed = [json.loads(r["row"]) if isinstance(r["row"], str) else r["row"] for r in rows]
    return TableSnapshot(columns=columns, rows=parsed)


async def source_revision(conn: DatabaseConnection, bank_id: str, row: dict[str, JsonValue]) -> SourceRevision:
    if not row["document_id"] or not row["chunk_id"]:
        raise CurationBatchConflict("Closure source has no document/chunk provenance")
    src = await conn.fetchrow(
        f"SELECT d.id,d.content_hash,d.updated_at,c.chunk_id,"
        "encode(sha256(convert_to(d.original_text,'UTF8')),'hex') AS document_hash,"
        "encode(sha256(convert_to(c.chunk_text,'UTF8')),'hex') AS chunk_hash,"
        "octet_length(d.original_text) AS document_bytes,octet_length(c.chunk_text) AS chunk_bytes "
        f"FROM {fq_table('documents')} d JOIN {fq_table('chunks')} c ON c.document_id=d.id AND c.bank_id=d.bank_id "
        "WHERE d.bank_id=$1 AND d.id=$2 AND c.chunk_id=$3",
        bank_id,
        row["document_id"],
        row["chunk_id"],
    )
    if (
        src is None
        or src["document_bytes"] is None
        or src["chunk_bytes"] is None
        or src["document_bytes"] > 1024 * 1024
        or src["chunk_bytes"] > 1024 * 1024
    ):
        raise CurationBatchConflict("Source missing or exceeds 1MiB cap")
    return SourceRevision(
        revision=revision({str(k): (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in dict(src).items()}),
        document_id=str(src["id"]),
        chunk_id=str(src["chunk_id"]),
        document_bytes=src["document_bytes"],
        chunk_bytes=src["chunk_bytes"],
    )


async def discover(conn: DatabaseConnection, bank_id: str, targets: list[UUID]) -> ClosureScope:
    ids = _ids(targets)
    raw = await conn.fetch(
        f"SELECT id,fact_type FROM {fq_table('memory_units')} WHERE bank_id=$1 AND id=ANY($2::uuid[])", bank_id, ids
    )
    if len(raw) != len(ids) or any(r["fact_type"] not in ("world", "experience") for r in raw):
        raise CurationBatchConflict("Targets must be live raw facts in the same bank")
    observations = await conn.fetch(
        f"SELECT id,source_memory_ids FROM {fq_table('memory_units')} WHERE bank_id=$1 AND fact_type='observation' "
        f"AND source_memory_ids && $2::uuid[] ORDER BY id LIMIT {MAX_OBSERVATIONS + 1}",
        bank_id,
        ids,
    )
    if len(observations) > MAX_OBSERVATIONS:
        raise CurationBatchConflict("Observation cap exceeded")
    obs_ids = _ids(r["id"] for r in observations)
    sources = _ids(s for r in observations for s in (r["source_memory_ids"] or []))
    if len(sources) > MAX_PEERS:
        raise CurationBatchConflict("Co-source cap exceeded")
    source_rows = await conn.fetch(
        f"SELECT id,fact_type FROM {fq_table('memory_units')} WHERE bank_id=$1 AND id=ANY($2::uuid[])", bank_id, sources
    )
    if len(source_rows) != len(sources) or any(r["fact_type"] not in ("world", "experience") for r in source_rows):
        raise CurationBatchConflict("Observation sources must be same-bank live raw facts")
    if obs_ids and await conn.fetchval(
        f"SELECT 1 FROM {fq_table('memory_units')} WHERE bank_id=$1 AND fact_type='observation' "
        "AND source_memory_ids && $2::uuid[] LIMIT 1",
        bank_id,
        obs_ids,
    ):
        raise CurationBatchConflict("Transitive observations are unsupported")
    affected = _ids([*ids, *obs_ids])
    if await conn.fetchval(
        f"SELECT 1 FROM {fq_table('memory_links')} WHERE bank_id IS DISTINCT FROM $1 "
        "AND (from_unit_id=ANY($2::uuid[]) OR to_unit_id=ANY($2::uuid[])) LIMIT 1",
        bank_id,
        affected,
    ) or await conn.fetchval(
        f"SELECT 1 FROM {fq_table('memory_units')} WHERE bank_id<>$1 AND fact_type='observation' "
        "AND source_memory_ids && $2::uuid[] LIMIT 1",
        bank_id,
        affected,
    ):
        raise CurationBatchConflict("Cross-bank dependency is outside the recoverable closure")
    links = await conn.fetch(
        f"SELECT from_unit_id,to_unit_id,entity_id FROM {fq_table('memory_links')} WHERE bank_id=$1 "
        f"AND (from_unit_id=ANY($2::uuid[]) OR to_unit_id=ANY($2::uuid[])) ORDER BY from_unit_id,to_unit_id,link_type LIMIT {MAX_LINKS + 1}",
        bank_id,
        affected,
    )
    if len(links) > MAX_LINKS:
        raise CurationBatchConflict("Link cap exceeded")
    peers = _ids([*sources, *(v for r in links for v in (r["from_unit_id"], r["to_unit_id"]))])
    peers = [v for v in peers if v not in affected]
    if len(peers) > MAX_PEERS:
        raise CurationBatchConflict("Peer cap exceeded")
    peer_count = await conn.fetchval(
        f"SELECT count(*) FROM {fq_table('memory_units')} WHERE bank_id=$1 AND id=ANY($2::uuid[])", bank_id, peers
    )
    if peer_count != len(peers):
        raise CurationBatchConflict("Graph peer is missing or belongs to another bank")
    postings = await conn.fetch(
        f"SELECT entity_id FROM {fq_table('unit_entities')} WHERE unit_id=ANY($1::uuid[])", affected
    )
    entities = _ids([*(r["entity_id"] for r in postings), *(r["entity_id"] for r in links if r["entity_id"])])
    if len(entities) > MAX_ENTITIES:
        raise CurationBatchConflict("Entity cap exceeded")
    return ClosureScope.model_validate({"targets": ids, "affected": affected, "peers": peers, "entities": entities})


async def capture(conn: DatabaseConnection, bank_id: str, scope: ClosureScope) -> CurationSnapshot:
    affected = _ids(scope.affected)
    all_ids = _ids([*scope.affected, *scope.peers])
    ents = _ids(scope.entities)
    memories = await table_snapshot(
        conn,
        "memory_units",
        f"SELECT to_jsonb(m) AS row FROM {fq_table('memory_units')} m WHERE bank_id=$1 AND id=ANY($2::uuid[]) ORDER BY id",
        bank_id,
        all_ids,
        cap=MAX_PEERS + 250,
    )
    archives = await table_snapshot(
        conn,
        "invalidated_memory_units",
        f"SELECT to_jsonb(m) AS row FROM {fq_table('invalidated_memory_units')} m WHERE bank_id=$1 AND id=ANY($2::uuid[]) ORDER BY id",
        bank_id,
        affected,
        cap=250,
    )
    postings = await table_snapshot(
        conn,
        "unit_entities",
        f"SELECT to_jsonb(u) AS row FROM {fq_table('unit_entities')} u WHERE unit_id=ANY($1::uuid[]) ORDER BY unit_id,entity_id",
        affected,
        cap=MAX_ENTITIES,
    )
    links = await table_snapshot(
        conn,
        "memory_links",
        f"SELECT to_jsonb(l) AS row FROM {fq_table('memory_links')} l WHERE bank_id=$1 AND (from_unit_id=ANY($2::uuid[]) OR to_unit_id=ANY($2::uuid[])) ORDER BY from_unit_id,to_unit_id,link_type",
        bank_id,
        affected,
        cap=MAX_LINKS,
    )
    entities = await table_snapshot(
        conn,
        "entities",
        f"SELECT to_jsonb(e) AS row FROM {fq_table('entities')} e WHERE bank_id=$1 AND id=ANY($2::uuid[]) ORDER BY id",
        bank_id,
        ents,
        cap=MAX_ENTITIES,
    )
    if len(entities.rows) != len(ents):
        raise CurationBatchConflict("Entity identity missing or belongs to another bank")
    cooccurrences = await table_snapshot(
        conn,
        "entity_cooccurrences",
        f"SELECT to_jsonb(c) AS row FROM {fq_table('entity_cooccurrences')} c WHERE entity_id_1=ANY($1::uuid[]) OR entity_id_2=ANY($1::uuid[]) ORDER BY entity_id_1,entity_id_2",
        ents,
        cap=MAX_LINKS,
    )
    history = await table_snapshot(
        conn,
        "observation_history",
        f"SELECT to_jsonb(h) AS row FROM {fq_table('observation_history')} h WHERE bank_id=$1 AND observation_id=ANY($2::uuid[]) ORDER BY id",
        bank_id,
        affected,
        cap=MAX_HISTORY,
    )
    sources: dict[str, SourceRevision] = {}
    cached_sources: dict[str, SourceRevision] = {}
    documents: dict[str, int] = {}
    chunks: dict[str, int] = {}
    source_bytes = 0
    for row in [*memories.rows, *archives.rows]:
        if row["fact_type"] not in ("world", "experience"):
            continue
        source_key = revision({"document_id": row["document_id"], "chunk_id": row["chunk_id"]})
        source = cached_sources.get(source_key)
        if source is None:
            source = await source_revision(conn, bank_id, row)
            cached_sources[source_key] = source
        sources[str(row["id"])] = source
        if source.document_id not in documents:
            documents[source.document_id] = source.document_bytes
            source_bytes += source.document_bytes
        if source.chunk_id not in chunks:
            chunks[source.chunk_id] = source.chunk_bytes
            source_bytes += source.chunk_bytes
        if source_bytes > MAX_BYTES:
            raise CurationBatchConflict("Source content exceeds 8MiB cap")
    dependencies = await table_snapshot(
        conn,
        "memory_units",
        f"SELECT to_jsonb(o) AS row FROM {fq_table('memory_units')} o WHERE bank_id=$1 AND fact_type='observation' AND (source_memory_ids && $2::uuid[] OR id=ANY($3::uuid[])) ORDER BY id",
        bank_id,
        all_ids,
        affected,
        cap=MAX_OBSERVATIONS,
    )
    schema = await conn.fetchval(
        f"SELECT string_agg(version_num,',' ORDER BY version_num) FROM {fq_table('alembic_version')}"
    )
    snapshot = CurationSnapshot(
        scope=scope,
        schema_revision=schema,
        memories=memories,
        archives=archives,
        postings=postings,
        links=links,
        entities=entities,
        cooccurrences=cooccurrences,
        history=history,
        source_revisions={i: s.revision for i, s in sources.items()},
        source_bytes=source_bytes,
        dependencies=dependencies.rows,
    )
    if len(canonical_bytes(snapshot)) + source_bytes > MAX_BYTES:
        raise CurationBatchConflict("Snapshot and source content exceed 8MiB cap")
    return snapshot


def inventory(snapshot: CurationSnapshot) -> CurationInventory:
    return CurationInventory(
        targets=len(snapshot.scope.targets),
        observations=len(snapshot.scope.affected) - len(snapshot.scope.targets),
        peers=len(snapshot.scope.peers),
        entities=len(snapshot.scope.entities),
        links=len(snapshot.links.rows),
        history_rows=len(snapshot.history.rows),
        snapshot_bytes=len(canonical_bytes(snapshot)),
        source_bytes=snapshot.source_bytes,
    )


def preview(snapshot: CurationSnapshot) -> CurationPreview:
    by_id = {r["id"]: r for r in snapshot.memories.rows}
    return CurationPreview(
        closure_revision=snapshot_revision(snapshot),
        targets=[
            CurationTargetRevision(
                memory_id=i, memory_revision=revision(by_id[str(i)]), source_revision=snapshot.source_revisions[str(i)]
            )
            for i in snapshot.scope.targets
        ],
        inventory=inventory(snapshot),
    )


async def get_capsule(conn: DatabaseConnection, bank_id: str, batch_id: str) -> BatchCapsule | None:
    value = await conn.fetchval(
        f"SELECT capsule FROM {fq_table('curation_batches')} WHERE bank_id=$1 AND batch_id=$2", bank_id, batch_id
    )
    if value is None:
        return None
    return BatchCapsule.model_validate_json(value) if isinstance(value, str) else BatchCapsule.model_validate(value)


async def assert_deletable(conn: DatabaseConnection, bank_id: str) -> None:
    if await conn.fetchval(
        f"SELECT 1 FROM {fq_table('curation_batches')} WHERE bank_id=$1 AND status='applied' LIMIT 1", bank_id
    ):
        raise CurationBatchConflict("Active curation capsules prevent bank deletion")


async def _posting_delta(
    conn: DatabaseConnection, bank_id: str, postings: list[dict[str, JsonValue]], sign: int
) -> None:
    counts = Counter(str(r["entity_id"]) for r in postings)
    for eid, count in sorted(counts.items()):
        value = await conn.fetchval(
            f"UPDATE {fq_table('entities')} SET mention_count=mention_count+$3 WHERE bank_id=$1 AND id=$2::uuid RETURNING mention_count",
            bank_id,
            eid,
            sign * count,
        )
        if value is None or value < 0:
            raise CurationBatchConflict("Entity posting counter conflict")


async def apply(
    conn: DatabaseConnection,
    bank_id: str,
    batch_id: str,
    request: CurationApplyRequest,
    before: CurationSnapshot,
    corrections: list[PreparedCorrection],
) -> CurationReceipt:
    expected = preview(before)
    if request.expected_closure_revision != expected.closure_revision:
        raise CurationBatchConflict("Closure revision changed")
    revisions = {str(r.memory_id): r for r in expected.targets}
    for change in request.changes:
        r = revisions[str(change.memory_id)]
        if change.memory_revision != r.memory_revision or change.source_revision != r.source_revision:
            raise CurationBatchConflict("Target/source revision changed")
    manifest_revision = revision(request)
    inv = inventory(before)
    debt = MaintenanceDebt(memory_ids=[*before.scope.affected, *before.scope.peers])
    # Capsule parent must exist before FK-backed pins. It is transaction-local,
    # and replaced with the complete before/after capsule before commit.
    await conn.execute(
        f"INSERT INTO {fq_table('curation_batches')}(bank_id,batch_id,manifest_revision,status,capsule) VALUES ($1,$2,$3,'applied','{{}}')",
        bank_id,
        batch_id,
        manifest_revision,
    )
    for eid in before.scope.entities:
        await conn.execute(
            f"INSERT INTO {fq_table('curation_entity_pins')}(bank_id,batch_id,entity_id) VALUES ($1,$2,$3)",
            bank_id,
            batch_id,
            str(eid),
        )
    target_ids = {str(i) for i in before.scope.targets}
    observations = [str(i) for i in before.scope.affected if str(i) not in target_ids]
    invalidated = [str(c.memory_id) for c in request.changes if c.action == "invalidate"]
    removed = {*observations, *invalidated}
    await _posting_delta(conn, bank_id, [r for r in before.postings.rows if r["unit_id"] in removed], -1)
    for change in request.changes:
        if change.action == "invalidate":
            if not await writes.invalidate_memory(
                conn=conn,
                fq_table=fq_table,
                bank_id=bank_id,
                unit_id=str(change.memory_id),
                reason=f"curation-batch:{batch_id}; {change.reason}",
            ):
                raise CurationBatchConflict("Raw target disappeared")
    await conn.execute(
        f"DELETE FROM {fq_table('memory_units')} WHERE bank_id=$1 AND id=ANY($2::uuid[])", bank_id, observations
    )
    await conn.execute(
        f"DELETE FROM {fq_table('observation_history')} WHERE bank_id=$1 AND observation_id=ANY($2::uuid[])",
        bank_id,
        observations,
    )
    for patch in corrections:
        await writes.apply_edit(
            conn=conn,
            fq_table=fq_table,
            bank_id=bank_id,
            unit_id=str(patch.memory_id),
            text=patch.text,
            context=patch.context,
            fact_type=patch.fact_type,
            occurred_start=patch.occurred_start,
            occurred_end=patch.occurred_end,
            event_date=patch.event_date,
            mentioned_at=None,
            entity_ids=None,
            embedding=patch.embedding,
        )
    # Surviving sources of retracted observations owe a future consolidation.
    co_sources = {
        str(i)
        for r in before.memories.rows
        if r["id"] in observations
        for i in TypeAdapter(list[UUID]).validate_python(r["source_memory_ids"] or [])
    } - removed
    await conn.execute(
        f"UPDATE {fq_table('memory_units')} SET consolidated_at=NULL,consolidation_failed_at=NULL WHERE bank_id=$1 AND id=ANY($2::uuid[])",
        bank_id,
        sorted(co_sources),
    )
    after = await capture(conn, bank_id, before.scope)
    receipt = CurationReceipt(
        bank_id=bank_id,
        batch_id=batch_id,
        manifest_revision=manifest_revision,
        receipt_revision=revision(
            {"manifest": manifest_revision, "after": snapshot_revision(after), "status": "applied"}
        ),
        status="applied",
        inventory=inv,
        maintenance_debt=debt,
    )
    capsule = BatchCapsule(
        manifest=request, before=before, after=after, receipt=receipt, applied_receipt_revision=receipt.receipt_revision
    )
    await conn.execute(
        f"UPDATE {fq_table('curation_batches')} SET capsule=$3::jsonb WHERE bank_id=$1 AND batch_id=$2",
        bank_id,
        batch_id,
        capsule.model_dump_json(),
    )
    return receipt


async def _insert_rows(
    conn: DatabaseConnection, table: str, snapshot: TableSnapshot, rows: list[dict[str, JsonValue]]
) -> None:
    if not rows:
        return
    columns = ",".join('"' + c.name.replace('"', '""') + '"' for c in snapshot.columns if not c.generated)
    await conn.execute(
        f"INSERT INTO {fq_table(table)} ({columns}) SELECT {columns} FROM jsonb_populate_recordset(NULL::{fq_table(table)},$1::jsonb)",
        json.dumps(rows),
    )


async def revert(
    conn: DatabaseConnection, bank_id: str, batch_id: str, capsule: BatchCapsule, expected_receipt: str
) -> CurationReceipt:
    if expected_receipt != capsule.applied_receipt_revision:
        raise CurationBatchConflict("Receipt revision changed")
    if capsule.receipt.status == "reverted":
        return capsule.receipt
    current = await capture(conn, bank_id, capsule.before.scope)
    if snapshot_revision(current) != snapshot_revision(capsule.after):
        raise CurationBatchConflict("Committed closure changed; capsule preserved")
    before = capsule.before
    affected = _ids(before.scope.affected)
    existing = {r["id"] for r in current.memories.rows}
    missing = [r for r in before.memories.rows if r["id"] not in existing]
    # All rows and peers are checked before reconstruction. Delete only our raw
    # archive rows, then reconstruct the captured live rows and exact histories.
    await conn.execute(
        f"DELETE FROM {fq_table('invalidated_memory_units')} WHERE bank_id=$1 AND id=ANY($2::uuid[])", bank_id, affected
    )
    await _insert_rows(conn, "memory_units", before.memories, missing)
    cols = [c.name for c in before.memories.columns if not c.generated and c.name not in ("id", "bank_id")]
    assignments = ",".join('"' + c + '"=r."' + c + '"' for c in cols)
    changed = [
        r
        for r in before.memories.rows
        if r["id"] in existing and r != next((a for a in current.memories.rows if a["id"] == r["id"]), None)
    ]
    if changed:
        await conn.execute(
            f"UPDATE {fq_table('memory_units')} m SET {assignments} FROM jsonb_populate_recordset(NULL::{fq_table('memory_units')},$3::jsonb) r WHERE m.bank_id=$1 AND r.bank_id=$1 AND m.id=r.id AND m.id=ANY($2::uuid[])",
            bank_id,
            _ids(str(r["id"]) for r in changed),
            json.dumps(changed),
        )
    # The CAS proved current associations unchanged since apply. Missing tuples
    # are ours, so only their actual insertion contributes a counter increment.
    present = {(r["unit_id"], r["entity_id"]) for r in current.postings.rows}
    restore_postings = [r for r in before.postings.rows if (r["unit_id"], r["entity_id"]) not in present]
    await _insert_rows(conn, "unit_entities", before.postings, restore_postings)
    await _posting_delta(conn, bank_id, restore_postings, 1)

    def link_key(row: dict[str, JsonValue]) -> str:
        return revision({key: row[key] for key in ("from_unit_id", "to_unit_id", "link_type")})

    present_links = {link_key(r) for r in current.links.rows}
    await _insert_rows(
        conn, "memory_links", before.links, [r for r in before.links.rows if link_key(r) not in present_links]
    )
    present_history = {r["id"] for r in current.history.rows}
    await _insert_rows(
        conn, "observation_history", before.history, [r for r in before.history.rows if r["id"] not in present_history]
    )
    restored = await capture(conn, bank_id, before.scope)
    if snapshot_revision(restored) != snapshot_revision(before):
        raise CurationBatchConflict("Restoration mismatch; transaction rolled back")
    receipt = capsule.receipt.model_copy(
        update={
            "status": "reverted",
            "receipt_revision": revision(
                {
                    "manifest": capsule.receipt.manifest_revision,
                    "restored": snapshot_revision(restored),
                    "status": "reverted",
                }
            ),
        }
    )
    capsule = capsule.model_copy(update={"receipt": receipt})
    await conn.execute(
        f"UPDATE {fq_table('curation_batches')} SET status='reverted',capsule=$3::jsonb,updated_at=now() WHERE bank_id=$1 AND batch_id=$2",
        bank_id,
        batch_id,
        capsule.model_dump_json(),
    )
    await conn.execute(
        f"DELETE FROM {fq_table('curation_entity_pins')} WHERE bank_id=$1 AND batch_id=$2", bank_id, batch_id
    )
    return receipt
