"""hindsight-admin rename-bank: move a bank to a new id in place.

Runs against an isolated, freshly migrated schema, so the catalog assertions see
exactly what the migrations produce and the index DDL never touches the shared
public.memory_units.
"""

import asyncio
import json
import uuid

from hindsight_api.admin import cli
from hindsight_api.engine.storage import bank_storage_prefix

import asyncpg
import pytest
import pytest_asyncio

from hindsight_api.admin.cli import _RIGID_BANK_ID_FKS_SQL, RenameBankError, _rename_bank, _run_rename_bank
from hindsight_api.engine.retain.bank_utils import _BANK_INDEX_FACT_TYPES, _vector_index_clause
from hindsight_api.engine.vector_index_health import plan_bank_vector_indexes, reconcile_bank_vector_indexes
from hindsight_api.migrations import run_migrations


@pytest_asyncio.fixture
async def rename_schema(pg0_db_url):
    schema = f"rename_test_{uuid.uuid4().hex[:8]}"
    conn = await asyncpg.connect(pg0_db_url)
    try:
        await conn.execute(f"CREATE SCHEMA {schema}")
    finally:
        await conn.close()
    run_migrations(pg0_db_url, schema=schema)
    conn = await asyncpg.connect(pg0_db_url)
    try:
        yield pg0_db_url, schema, conn
    finally:
        await conn.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        await conn.close()


async def _seed_bank(conn: asyncpg.Connection, schema: str, bank_id: str) -> None:
    """A bank with rows behind each composite (id, bank_id) FK."""
    doc_id = f"doc-{uuid.uuid4().hex[:8]}"
    mm_id = f"mm-{uuid.uuid4().hex[:8]}"
    await conn.execute(f"INSERT INTO {schema}.banks (bank_id) VALUES ($1)", bank_id)
    await conn.execute(
        f"INSERT INTO {schema}.documents (id, bank_id, original_text) VALUES ($1, $2, 'text')", doc_id, bank_id
    )
    await conn.execute(
        f"""INSERT INTO {schema}.memory_units (bank_id, document_id, text, fact_type, event_date)
            VALUES ($1, $2, 'Alice likes tea', 'world', NOW())""",
        bank_id,
        doc_id,
    )
    await conn.execute(
        f"""INSERT INTO {schema}.mental_models (id, bank_id, subtype, name, source_query, content)
            VALUES ($1, $2, 'pinned', 'Model', 'q?', 'c')""",
        mm_id,
        bank_id,
    )
    await conn.execute(
        f"""INSERT INTO {schema}.mental_model_history (mental_model_id, bank_id, content)
            VALUES ($1, $2, '{{}}'::jsonb)""",
        mm_id,
        bank_id,
    )
    await conn.execute(
        f"INSERT INTO {schema}.directives (bank_id, name, content) VALUES ($1, 'tone', 'be brief')", bank_id
    )


async def _counts(conn: asyncpg.Connection, schema: str, bank_id: str) -> dict[str, int]:
    # Raw SQL on purpose: the property under test is that no table in the schema
    # keeps a row under the old id, which no engine read API can enumerate.
    tables = await conn.fetch(
        """SELECT c.table_name FROM information_schema.columns c
           JOIN information_schema.tables t USING (table_schema, table_name)
           WHERE c.table_schema = $1 AND c.column_name = 'bank_id' AND t.table_type = 'BASE TABLE'""",
        schema,
    )
    return {
        r["table_name"]: await conn.fetchval(
            f"SELECT count(*) FROM {schema}.{r['table_name']} WHERE bank_id = $1", bank_id
        )
        for r in tables
    }


@pytest.mark.asyncio
async def test_rename_restores_fk_deferrability(rename_schema):
    """The FKs are made DEFERRABLE only for the rename; the schema ends as the migrations declared it."""
    _, schema, conn = rename_schema
    await _seed_bank(conn, schema, "old")
    rigid_before = sorted(tuple(r) for r in await conn.fetch(_RIGID_BANK_ID_FKS_SQL, schema))
    assert rigid_before, "expected NOT DEFERRABLE bank_id FKs in a freshly migrated schema"

    await _rename_bank(conn, schema, "old", "new", dry_run=False)
    with pytest.raises(RenameBankError):
        await _rename_bank(conn, schema, "old", "newer", dry_run=False)

    assert sorted(tuple(r) for r in await conn.fetch(_RIGID_BANK_ID_FKS_SQL, schema)) == rigid_before


@pytest.mark.asyncio
async def test_rename_moves_every_row_and_dry_run_moves_none(rename_schema):
    _, schema, conn = rename_schema
    await _seed_bank(conn, schema, "old")
    await _seed_bank(conn, schema, "bystander")
    before = await _counts(conn, schema, "old")
    bystander = await _counts(conn, schema, "bystander")
    expected = {t: n for t, n in before.items() if n}

    assert await _rename_bank(conn, schema, "old", "new", dry_run=True) == expected
    assert await _counts(conn, schema, "old") == before

    assert await _rename_bank(conn, schema, "old", "new", dry_run=False) == expected
    assert await _counts(conn, schema, "new") == before
    assert not any((await _counts(conn, schema, "old")).values())
    assert await _counts(conn, schema, "bystander") == bystander


@pytest.mark.asyncio
async def test_rename_refuses_without_changing_anything(rename_schema):
    _, schema, conn = rename_schema
    await _seed_bank(conn, schema, "old")
    await _seed_bank(conn, schema, "taken")
    await conn.execute(
        f"""INSERT INTO {schema}.async_operations (operation_id, bank_id, operation_type, status)
            VALUES ($1, 'old', 'consolidation', 'completed')""",
        uuid.uuid4(),
    )
    before = await _counts(conn, schema, "old")

    with pytest.raises(RenameBankError, match="does not exist"):
        await _rename_bank(conn, schema, "missing", "new", dry_run=False)
    with pytest.raises(RenameBankError, match="already exists"):
        await _rename_bank(conn, schema, "old", "taken", dry_run=False)

    await conn.execute(f"UPDATE {schema}.async_operations SET status = 'pending' WHERE bank_id = 'old'")
    with pytest.raises(RenameBankError, match="pending or processing"):
        await _rename_bank(conn, schema, "old", "new", dry_run=False)

    assert await _counts(conn, schema, "old") == before


@pytest.mark.asyncio
async def test_rename_rebuilds_vector_indexes_for_the_new_id(rename_schema):
    """The per-bank indexes are partial on the bank_id literal: left alone they cover nothing."""
    db_url, schema, conn = rename_schema
    index_clause = _vector_index_clause()
    if index_clause is None:
        pytest.skip("configured vector backend has no per-bank indexes")
    await _seed_bank(conn, schema, "old")
    built = await reconcile_bank_vector_indexes(conn, schema, "old", index_clause)
    assert built.created == len(_BANK_INDEX_FACT_TYPES)

    await _run_rename_bank(db_url, schema, "old", "new", dry_run=False)

    plan = await plan_bank_vector_indexes(conn, schema, "new")
    assert plan.is_empty and plan.already_present == len(_BANK_INDEX_FACT_TYPES)
    defs = await conn.fetch(
        "SELECT indexdef FROM pg_indexes WHERE schemaname = $1 AND indexname LIKE 'idx_mu_emb_%'", schema
    )
    assert defs and all("'new'::text" in r["indexdef"] for r in defs)


async def _seed_files(conn, schema):
    await _seed_bank(conn, schema, "old")
    prefix = bank_storage_prefix("old", schema)
    storage = cli._admin_file_storage(conn, schema)
    for suffix in ["retry.pdf", "document.pdf", "attachment.png", "export.zip", "unknown.bin"]:
        await storage.store(key=prefix + suffix, file_data=(suffix + "\x00original").encode())
    await conn.execute(
        f"UPDATE {schema}.documents SET file_storage_key=$1 WHERE bank_id='old'", prefix + "document.pdf"
    )
    await conn.execute(
        f"INSERT INTO {schema}.attachments (bank_id,document_id,attachment_hash,short_id,storage_key,media_type,byte_size) "
        f"SELECT bank_id,id,'abc','abc',$1,'image/png',28 FROM {schema}.documents WHERE bank_id='old'",
        prefix + "attachment.png",
    )
    ids = {}
    for status in ["failed", "cancelled", "completed"]:
        ids[status] = uuid.uuid4()
        payload = {
            "bank_id": "old",
            "storage_key": prefix + "retry.pdf",
            "type": "file_convert_retain",
            "document_id": "doc-1",
            "_file_metadata": {"file_storage_key": prefix + "document.pdf", "filename": "old evidence.pdf"},
            "operation_id": str(ids[status]),
            "metadata": {"bank_id": "old", "text": prefix},
        }
        await conn.execute(
            f"INSERT INTO {schema}.async_operations (operation_id,bank_id,operation_type,status,task_payload,result_metadata) "
            "VALUES ($1,'old','file_convert_retain',$2,$3::jsonb,$4::jsonb)",
            ids[status],
            status,
            json.dumps(payload),
            json.dumps({"bank_id": "old", "storage_key": prefix + "export.zip"}),
        )
    return storage, prefix, ids


async def _snapshot(conn, schema):
    tables = await conn.fetch("SELECT tablename FROM pg_tables WHERE schemaname=$1 ORDER BY tablename", schema)
    data = {}
    for row in tables:
        data[row["tablename"]] = sorted(
            json.dumps(value, sort_keys=True)
            for value in json.loads(
                await conn.fetchval(
                    f"SELECT coalesce(jsonb_agg(to_jsonb(t)),'[]')::text FROM {schema}.{row['tablename']} t"
                )
            )
        )
    flags = [
        tuple(r)
        for r in await conn.fetch(
            "SELECT conname,condeferrable,condeferred FROM pg_constraint WHERE connamespace=$1::regnamespace ORDER BY conname",
            schema,
        )
    ]
    return data, flags


@pytest.mark.asyncio
async def test_retry_destinations_files_and_evidence_survive(rename_schema):
    db, schema, conn = rename_schema
    storage, prefix, ids = await _seed_files(conn, schema)
    before = {r["operation_id"]: dict(r) for r in await conn.fetch(f"SELECT * FROM {schema}.async_operations")}
    originals = {
        r["storage_key"]: await storage.retrieve(r["storage_key"])
        for r in await conn.fetch(f"SELECT * FROM {schema}.file_storage")
    }
    identities = await conn.fetch(f"SELECT id FROM {schema}.memory_units WHERE bank_id='old' ORDER BY id")
    await _seed_bank(conn, schema, "other")
    await conn.execute(
        f"INSERT INTO {schema}.curation_batches(bank_id,batch_id,manifest_revision,status,capsule) "
        "VALUES ('other','other-batch','revision','applied','{\"bank_id\":\"old\",\"hash\":\"immutable\"}')"
    )
    other = await conn.fetchval(f"SELECT row_to_json(c)::text FROM {schema}.curation_batches c WHERE bank_id='other'")
    await _run_rename_bank(db, schema, "old", "new", dry_run=False)
    rows = {r["operation_id"]: dict(r) for r in await conn.fetch(f"SELECT * FROM {schema}.async_operations")}
    for status, op_id in ids.items():
        assert rows[op_id]["bank_id"] == "new"
        payload = json.loads(rows[op_id]["task_payload"])
        if status == "completed":
            assert rows[op_id]["task_payload"] == before[op_id]["task_payload"]
        else:
            assert payload["bank_id"] == "new"
            assert payload["storage_key"] == bank_storage_prefix("new", schema) + "retry.pdf"
            assert await storage.retrieve(payload["storage_key"]) == originals[prefix + "retry.pdf"]
            assert payload["metadata"] == {"bank_id": "old", "text": prefix}
            assert payload["operation_id"] == str(op_id)
            assert payload["_file_metadata"] == {
                "file_storage_key": bank_storage_prefix("new", schema) + "document.pdf",
                "filename": "old evidence.pdf",
            }
        assert rows[op_id]["result_metadata"] == before[op_id]["result_metadata"]
    for key, value in originals.items():
        assert await storage.retrieve(key) == value
    assert await conn.fetch(f"SELECT id FROM {schema}.memory_units WHERE bank_id='new' ORDER BY id") == identities
    assert await conn.fetchval(f"SELECT id FROM {schema}.documents WHERE bank_id='new'") == "doc-1"
    assert (
        await conn.fetchval(f"SELECT row_to_json(c)::text FROM {schema}.curation_batches c WHERE bank_id='other'")
        == other
    )


@pytest.mark.asyncio
async def test_retry_retarget_preserves_native_jsonb_precision(rename_schema):
    db, schema, conn = rename_schema
    _, _, ids = await _seed_files(conn, schema)
    await conn.execute(
        f"UPDATE {schema}.async_operations SET task_payload = "
        "jsonb_set(task_payload, '{metadata,precise}', '0.12345678901234567890123456789'::jsonb) "
        "WHERE operation_id = $1",
        ids["failed"],
    )
    before = await conn.fetchval(
        f"SELECT (task_payload->'metadata')::text FROM {schema}.async_operations WHERE operation_id=$1",
        ids["failed"],
    )
    await _run_rename_bank(db, schema, "old", "new", dry_run=False)
    assert (
        await conn.fetchval(
            f"SELECT (task_payload->'metadata')::text FROM {schema}.async_operations WHERE operation_id=$1",
            ids["failed"],
        )
        == before
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("disposition", ["owned", "unreachable"])
async def test_target_store_preflight_has_zero_effects(rename_schema, monkeypatch, disposition):
    db, schema, conn = rename_schema
    await _seed_files(conn, schema)
    before = await _snapshot(conn, schema)
    probes = []

    def probe(bank_id):
        probes.append(bank_id)
        if bank_id == "new":
            if disposition == "unreachable":
                raise RuntimeError("target store unavailable")
            return True
        return False

    async def unexpected_connect(*args):
        pytest.fail("store preflight must run before connecting or mutating")

    monkeypatch.setattr(cli, "bank_indexes_are_store_owned", probe)
    monkeypatch.setattr(cli, "_admin_connect", unexpected_connect)
    with pytest.raises(RenameBankError, match="outside SQL|cannot tell whether"):
        await _run_rename_bank(db, schema, "old", "new", dry_run=False)
    assert probes == ["old", "new"]
    assert await _snapshot(conn, schema) == before


@pytest.mark.asyncio
async def test_dry_run_never_executes_updates_or_catalog_ddl(rename_schema):
    db, schema, conn = rename_schema
    await _seed_files(conn, schema)
    await conn.execute(f"CREATE SEQUENCE {schema}.update_calls")
    await conn.execute(f"""CREATE FUNCTION {schema}.count_update() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN PERFORM nextval('{schema}.update_calls'); RETURN NEW; END $$""")
    await conn.execute(
        f"CREATE TRIGGER count_update BEFORE UPDATE ON {schema}.banks FOR EACH ROW EXECUTE FUNCTION {schema}.count_update()"
    )
    before = await _snapshot(conn, schema)
    seq = await conn.fetchval(f"SELECT last_value FROM {schema}.update_calls")
    await _run_rename_bank(db, schema, "old", "new", dry_run=True)
    assert await _snapshot(conn, schema) == before
    assert await conn.fetchval(f"SELECT last_value FROM {schema}.update_calls") == seq
    assert not await conn.fetchval(f"SELECT is_called FROM {schema}.update_calls")


@pytest.mark.asyncio
@pytest.mark.parametrize("collision", ["bank", "alias", "file"])
async def test_target_collision_has_zero_effects(rename_schema, collision):
    db, schema, conn = rename_schema
    storage, _, _ = await _seed_files(conn, schema)
    if collision == "bank":
        await _seed_bank(conn, schema, "new")
    elif collision == "alias":
        await conn.execute(f"INSERT INTO {schema}.bank_aliases(alias,bank_id) VALUES ('new','old')")
    else:
        await storage.store(key=bank_storage_prefix("new", schema) + "retry.pdf", file_data=b"destination")
    before = await _snapshot(conn, schema)
    with pytest.raises(RenameBankError, match="already exists|collision"):
        await _run_rename_bank(db, schema, "old", "new", dry_run=False)
    assert await _snapshot(conn, schema) == before


@pytest.mark.asyncio
async def test_applied_capsules_fail_closed_without_rewriting_hashes(rename_schema):
    db, schema, conn = rename_schema
    await _seed_files(conn, schema)
    await conn.execute(
        f"INSERT INTO {schema}.curation_batches(bank_id,batch_id,manifest_revision,status,capsule) "
        "VALUES ('old','stable-batch','revision','applied','{\"bank_id\":\"old\",\"hash\":\"commitment\"}')"
    )
    before = await _snapshot(conn, schema)
    with pytest.raises(RenameBankError, match="curation"):
        await _run_rename_bank(db, schema, "old", "new", dry_run=False)
    assert await _snapshot(conn, schema) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["repoint", "commit"])
async def test_row_or_commit_failure_rolls_back_schema_rows_and_native_blobs(rename_schema, stage):
    db, schema, conn = rename_schema
    await _seed_files(conn, schema)
    await conn.execute(f"""CREATE FUNCTION {schema}.reject_rename() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN RAISE EXCEPTION 'injected rename failure'; END $$""")
    if stage == "commit":
        await conn.execute(
            f"CREATE CONSTRAINT TRIGGER reject_rename AFTER UPDATE ON {schema}.banks "
            f"DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION {schema}.reject_rename()"
        )
    else:
        await conn.execute(
            f"CREATE TRIGGER reject_rename BEFORE UPDATE ON {schema}.documents "
            f"FOR EACH ROW EXECUTE FUNCTION {schema}.reject_rename()"
        )
    before = await _snapshot(conn, schema)
    with pytest.raises(Exception, match="injected rename failure"):
        await _run_rename_bank(db, schema, "old", "new", dry_run=False)
    assert await _snapshot(conn, schema) == before


@pytest.mark.asyncio
async def test_rename_excludes_retry_claim_document_and_native_file_writers(rename_schema, monkeypatch):
    from unittest.mock import AsyncMock
    from hindsight_api.engine.db import create_database_backend
    from hindsight_api.worker import WorkerPoller

    poller = WorkerPoller(
        backend=create_database_backend("postgresql"), worker_id="rename-barrier", executor=AsyncMock()
    )
    db, schema, conn = rename_schema
    _, prefix, ids = await _seed_files(conn, schema)
    held, release = asyncio.Event(), asyncio.Event()
    move = cli._move_bank_files

    async def pause(*args, **kwargs):
        held.set()
        await release.wait()
        return await move(*args, **kwargs)

    monkeypatch.setattr(cli, "_move_bank_files", pause)
    rename = asyncio.create_task(_run_rename_bank(db, schema, "old", "new", dry_run=False))
    writers, jobs = [], []
    try:
        await asyncio.wait_for(held.wait(), 10)
        statements = [
            (
                f"UPDATE {schema}.async_operations SET status='pending' WHERE operation_id=$1 AND bank_id='old'",
                (ids["failed"],),
            ),
            (None, ()),
            (f"UPDATE {schema}.documents SET original_text='writer' WHERE bank_id='old'", ()),
            (f"INSERT INTO {schema}.file_storage(storage_key,data) VALUES($1,$2)", (prefix + "late", b"late bytes")),
        ]
        for statement, args in statements:
            writer = await asyncpg.connect(db)
            writers.append(writer)
            if statement is None:
                jobs.append(asyncio.create_task(poller._claim_batch_for_schema_inner(writer, schema, {}, 1)))
            else:
                jobs.append(asyncio.create_task(writer.execute(statement, *args)))
        async with asyncio.timeout(5):
            for writer in writers:
                while not await conn.fetchval(
                    "SELECT EXISTS(SELECT 1 FROM pg_locks WHERE pid=$1 AND NOT granted)", writer.get_server_pid()
                ):
                    await asyncio.sleep(0)
        assert all(not job.done() for job in jobs)
    finally:
        release.set()
        await asyncio.gather(rename, *jobs, return_exceptions=True)
        for writer in writers:
            await writer.close()
    assert rename.exception() is None
    assert jobs[0].result() == "UPDATE 0"
    assert jobs[1].result() == []
    assert jobs[2].result() == "UPDATE 0"
    assert (
        json.loads(
            await conn.fetchval(
                f"SELECT task_payload FROM {schema}.async_operations WHERE operation_id=$1", ids["failed"]
            )
        )["bank_id"]
        == "new"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("reported", [False, True])
async def test_index_failure_is_not_a_success_and_recovery_is_schema_qualified(rename_schema, monkeypatch, reported):
    from types import SimpleNamespace

    db, schema, conn = rename_schema
    await _seed_bank(conn, schema, "old")
    reconcile = cli.reconcile_bank_vector_indexes

    async def fail(*args):
        if reported:
            return SimpleNamespace(created=0, dropped=0, failed=1)
        raise RuntimeError("injected index failure")

    monkeypatch.setattr(cli, "reconcile_bank_vector_indexes", fail)
    with pytest.raises(RenameBankError, match=f"repair-bank.*--bank new.*--schema {schema}"):
        await _run_rename_bank(db, schema, "old", "new", dry_run=False)
    assert await conn.fetchval(f"SELECT bank_id FROM {schema}.banks") == "new"
    monkeypatch.setattr(cli, "reconcile_bank_vector_indexes", reconcile)
    repaired = await cli._run_repair_bank(db, base_schema=schema, schema=schema, bank_id="new", dry_run=False)
    assert not repaired.skipped_schemas and not any(r.failed for r in repaired.banks)
    defs = await conn.fetch(
        "SELECT indexdef FROM pg_indexes WHERE schemaname=$1 AND indexname LIKE 'idx_mu_emb_%'", schema
    )
    assert defs and all("'new'::text" in row["indexdef"] for row in defs)


@pytest.mark.asyncio
async def test_native_retry_and_worker_claim_execute_the_retargeted_payload(rename_schema, monkeypatch):
    from unittest.mock import AsyncMock
    from hindsight_api.engine.db import create_database_backend
    from hindsight_api.engine.memory_engine import MemoryEngine, _current_schema
    from hindsight_api.models import RequestContext
    from hindsight_api.worker import WorkerPoller

    db, schema, conn = rename_schema
    storage, _, ids = await _seed_files(conn, schema)
    await _run_rename_bank(db, schema, "old", "new", dry_run=False)
    backend = create_database_backend("postgresql")
    await backend.initialize(db, min_size=1, max_size=2)
    token = _current_schema.set(schema)
    try:
        # Authentication is outside this fixture's boundary. Use the real retry
        # transition and worker SQL, against the fixture's native backend.
        engine = MemoryEngine.__new__(MemoryEngine)
        monkeypatch.setattr(engine, "_get_backend", AsyncMock(return_value=backend))
        monkeypatch.setattr(engine, "_authenticate_tenant", AsyncMock())
        monkeypatch.setattr(engine, "_operation_validator", None, raising=False)
        reply = await engine.retry_operation("new", str(ids["failed"]), request_context=RequestContext())
        assert reply["operation_id"] == str(ids["failed"])
        with pytest.raises(ValueError, match="not found for bank old"):
            await engine.retry_operation("old", str(ids["cancelled"]), request_context=RequestContext())
        poller = WorkerPoller(backend=backend, worker_id="rename-proof", executor=AsyncMock())
        async with backend.acquire() as claimed_conn:
            tasks = await poller._claim_batch_for_schema_inner(claimed_conn, schema, {}, 1)
        assert len(tasks) == 1
        assert tasks[0].operation_id == str(ids["failed"])
        assert tasks[0].task_dict["bank_id"] == "new"
        assert await storage.retrieve(tasks[0].task_dict["storage_key"]) == b"retry.pdf\x00original"
    finally:
        _current_schema.reset(token)
        await backend.shutdown()


@pytest.mark.asyncio
async def test_fk_restore_failure_rolls_back_everything(rename_schema, monkeypatch):
    db, schema, conn = rename_schema
    await _seed_files(conn, schema)
    before = await _snapshot(conn, schema)
    restore = cli._set_fks_deferrable

    async def fail_restore(conn, fks, clause):
        if clause == "NOT DEFERRABLE":
            raise RuntimeError("injected catalog restore failure")
        await restore(conn, fks, clause)

    monkeypatch.setattr(cli, "_set_fks_deferrable", fail_restore)
    with pytest.raises(RuntimeError, match="catalog restore"):
        await _run_rename_bank(db, schema, "old", "new", dry_run=False)
    assert await _snapshot(conn, schema) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("ack_lost", [False, True])
async def test_commit_failure_or_lost_ack_preserves_source_and_consistent_references(
    rename_schema, monkeypatch, ack_lost
):
    from asyncpg.transaction import Transaction

    db, schema, conn = rename_schema
    storage, prefix, ids = await _seed_files(conn, schema)
    before = await _snapshot(conn, schema)
    commit = Transaction._Transaction__commit

    async def lose_commit(transaction):
        if transaction._nested:
            return await commit(transaction)
        if ack_lost:
            await commit(transaction)
        raise RuntimeError("injected commit transport failure")

    monkeypatch.setattr(Transaction, "_Transaction__commit", lose_commit)
    with pytest.raises(RuntimeError, match="commit transport"):
        await _run_rename_bank(db, schema, "old", "new", dry_run=False)
    assert await storage.retrieve(prefix + "retry.pdf") == b"retry.pdf\x00original"
    if not ack_lost:
        assert await _snapshot(conn, schema) == before
    else:
        assert await conn.fetchval(f"SELECT bank_id FROM {schema}.banks") == "new"
        payload = json.loads(
            await conn.fetchval(
                f"SELECT task_payload FROM {schema}.async_operations WHERE operation_id=$1", ids["failed"]
            )
        )
        assert payload["bank_id"] == "new"
        assert await storage.retrieve(payload["storage_key"]) == b"retry.pdf\x00original"
        assert (await _snapshot(conn, schema))[1] == before[1]


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["native", "object"])
async def test_copy_failure_preserves_originals_and_does_not_publish_partial_references(
    rename_schema, monkeypatch, backend
):
    from hindsight_api.engine.storage.base import ObstoreFileStorage
    from obstore.store import MemoryStore

    db, schema, conn = rename_schema
    native, prefix, _ = await _seed_files(conn, schema)
    originals = {
        r["storage_key"]: await native.retrieve(r["storage_key"])
        for r in await conn.fetch(f"SELECT storage_key FROM {schema}.file_storage")
    }
    factory = cli._admin_file_storage
    copied = []

    def fail_on_second_copy(storage):
        create = storage.store_new

        async def fail_second(file_data, key):
            if copied:
                raise RuntimeError("injected second copy failure")
            await create(file_data, key)
            copied.append(key)
            return key

        monkeypatch.setattr(storage, "store_new", fail_second)
        return storage

    if backend == "object":
        storage = ObstoreFileStorage()
        storage._store = MemoryStore()
        for key, data in originals.items():
            await storage.store(data, key)
        fail_on_second_copy(storage)
        monkeypatch.setattr(cli, "_admin_file_storage", lambda *_: storage)
    else:
        storage = native
        # Keep production's borrowed transaction connection, not the observer
        # connection used to seed and read back the fixture.
        monkeypatch.setattr(cli, "_admin_file_storage", lambda conn, schema: fail_on_second_copy(factory(conn, schema)))
    before = await _snapshot(conn, schema)
    with pytest.raises(RuntimeError, match="second copy"):
        await _run_rename_bank(db, schema, "old", "new", dry_run=False)
    assert copied
    assert await _snapshot(conn, schema) == before
    for key, data in originals.items():
        assert await storage.retrieve(key) == data
    if backend == "object":
        # External creates cannot join a PG rollback. The safe residue is a
        # destination orphan, never a deleted source or a repointed database row.
        assert await storage.retrieve(copied[0])
        with pytest.raises(RenameBankError, match="collision"):
            await _run_rename_bank(db, schema, "old", "new", dry_run=False)


@pytest.mark.asyncio
async def test_object_destination_race_is_create_only(rename_schema, monkeypatch):
    from hindsight_api.engine.storage.base import ObstoreFileStorage
    from obstore.store import MemoryStore

    db, schema, conn = rename_schema
    native, _, _ = await _seed_files(conn, schema)
    storage = ObstoreFileStorage()
    storage._store = MemoryStore()
    originals = {
        r["storage_key"]: await native.retrieve(r["storage_key"])
        for r in await conn.fetch(f"SELECT storage_key FROM {schema}.file_storage")
    }
    for key, data in originals.items():
        await storage.store(data, key)
    monkeypatch.setattr(cli, "_admin_file_storage", lambda *_: storage)
    before = await _snapshot(conn, schema)
    create = storage.store_new
    targets = []

    async def racing_create(file_data, key):
        await storage.store(b"competing destination", key)
        targets.append(key)
        return await create(file_data, key)

    monkeypatch.setattr(storage, "store_new", racing_create)
    with pytest.raises(FileExistsError):
        await _run_rename_bank(db, schema, "old", "new", dry_run=False)
    assert await _snapshot(conn, schema) == before
    assert len(targets) == 1
    assert await storage.retrieve(targets[0]) == b"competing destination"
    for key, data in originals.items():
        assert await storage.retrieve(key) == data


@pytest.mark.asyncio
async def test_cli_reports_committed_index_failure_and_native_repair_recovers(rename_schema, monkeypatch):
    from typer.testing import CliRunner

    db, schema, conn = rename_schema
    await _seed_bank(conn, schema, "old")
    reconcile = cli.reconcile_bank_vector_indexes

    async def fail(*args, **kwargs):
        raise RuntimeError("injected index failure")

    monkeypatch.setenv("HINDSIGHT_API_DATABASE_URL", db)
    monkeypatch.setattr(cli, "reconcile_bank_vector_indexes", fail)
    result = await asyncio.to_thread(
        CliRunner().invoke, cli.app, ["rename-bank", "--from", "old", "--to", "new", "--schema", schema]
    )
    assert result.exit_code == 1
    assert "Rename committed" in result.output
    assert f"repair-bank --bank new --schema {schema}" in result.output
    monkeypatch.setattr(cli, "reconcile_bank_vector_indexes", reconcile)
    repaired = await asyncio.to_thread(
        CliRunner().invoke, cli.app, ["repair-bank", "--bank", "new", "--schema", schema]
    )
    assert repaired.exit_code == 0, repaired.output
    assert await conn.fetchval(f"SELECT bank_id FROM {schema}.banks") == "new"
