"""Graph deadlines must not leave an implicit asyncpg transaction in the pool."""

import asyncio
import uuid
from contextlib import suppress
from types import SimpleNamespace

import asyncpg
import pytest

from hindsight_api.engine.db.ops import LinkExpansionRows
from hindsight_api.engine.db.ops_postgresql import PostgreSQLOps
from hindsight_api.engine.db.postgresql import _NoResetConnection
from hindsight_api.engine.memories.pg import link_expansion
from hindsight_api.engine.memories.pg.link_expansion import LinkExpansionRetriever


@pytest.mark.asyncio
@pytest.mark.parametrize("arm", ["observation", "combined"])
async def test_observation_timeout_after_prepare_leaves_no_server_transaction(pg0_db_url, monkeypatch, arm):
    # A private relation gives this test the same DDL/reader interaction as bank
    # deletion without blocking unrelated test banks on public.memory_units.
    schema = f"timeout_cleanup_{uuid.uuid4().hex}"
    internal_id = str(uuid.uuid4())
    index = f"idx_mu_emb_w_{internal_id.replace('-', '')[:16]}"
    ops = PostgreSQLOps()
    observer = await asyncpg.connect(pg0_db_url)
    ddl_conn = await asyncpg.connect(pg0_db_url)
    await observer.execute(f"CREATE SCHEMA {schema}")
    await observer.execute(f"CREATE TABLE {schema}.memory_units (id uuid)")
    await observer.execute(f"CREATE INDEX {index} ON {schema}.memory_units (id)")
    pool = await asyncpg.create_pool(
        pg0_db_url, min_size=1, max_size=1, statement_cache_size=0, connection_class=_NoResetConnection
    )
    prepared = asyncio.Event()
    original_get_statement = asyncpg.Connection._get_statement
    raw_connection = None
    drop = None

    async def pause_after_prepare(self, *args, **kwargs):
        statement = await original_get_statement(self, *args, **kwargs)
        if self is raw_connection and not prepared.is_set():
            prepared.set()
            await asyncio.Future()
        return statement

    async def deadline_after_prepare(coro, *, timeout):
        nonlocal drop
        task = asyncio.create_task(coro)
        try:
            await prepared.wait()
        finally:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        # PREPARE/Flush has opened a server transaction, but without Sync the
        # driver still thinks it is idle. Prove the actual bank-delete DDL waits
        # on this backend, not just that the backend's state looks suspicious.
        # Until Sync, PostgreSQL can report this as active/ClientRead rather
        # than the literal idle-in-transaction state. Require the parked server
        # transaction and then prove its exact blocking role, not a state label.
        activity = None
        try:
            async with asyncio.timeout(5):
                while True:
                    activity = await observer.fetchrow(
                        "SELECT state, xact_start, wait_event_type, wait_event FROM pg_stat_activity WHERE pid = $1",
                        pid,
                    )
                    if (
                        activity["state"] in {"active", "idle in transaction"}
                        and activity["xact_start"] is not None
                        and activity["wait_event_type"] == "Client"
                        and activity["wait_event"] == "ClientRead"
                    ):
                        break
                    await asyncio.sleep(0.01)
        except TimeoutError:
            pytest.fail("Cancelled PREPARE did not leave a server transaction parked on ClientRead")
        assert activity is not None
        assert raw_connection is not None
        assert not raw_connection.is_in_transaction()
        drop = asyncio.create_task(ops.drop_bank_vector_indexes(ddl_conn, schema, internal_id, {"world": "w"}))
        wait = None
        try:
            async with asyncio.timeout(5):
                while True:
                    wait = await observer.fetchrow(
                        "SELECT state, wait_event_type, wait_event, pg_blocking_pids(pid) AS blockers "
                        "FROM pg_stat_activity WHERE pid = $1",
                        ddl_conn.get_server_pid(),
                    )
                    if pid in wait["blockers"]:
                        break
                    await asyncio.sleep(0.01)
        except TimeoutError:
            pytest.fail("Production DROP never blocked on the cancelled PREPARE backend")
        assert wait is not None
        print(f"PREPARE backend={pid}: {dict(activity)}; DROP backend={ddl_conn.get_server_pid()}: {dict(wait)}")
        assert wait["wait_event_type"] == "Lock"
        assert not drop.done()
        raise TimeoutError

    async def expand(conn, *_args):
        # Raw SQL is necessary to force the protocol boundary and relation locks;
        # no public memory read API exposes an unfinished PREPARE.
        return await conn.fetch(f"SELECT id FROM {schema}.memory_units WHERE id = $1::uuid", None)

    monkeypatch.setattr(asyncpg.Connection, "_get_statement", pause_after_prepare)
    monkeypatch.setattr(
        link_expansion, "asyncio", SimpleNamespace(wait_for=deadline_after_prepare, TimeoutError=TimeoutError)
    )
    try:
        async with pool.acquire() as conn:
            raw_connection = conn._con
            pid = conn.get_server_pid()
            retriever = LinkExpansionRetriever()
            if arm == "observation":
                rows = await retriever._expand_observations(
                    conn, [], 2, ops=SimpleNamespace(expand_observations=expand)
                )
                assert rows == LinkExpansionRows(entity=[], semantic=[], causal=[])
            else:
                # Exercise the sibling's *real* fallback fetch: its Sync must
                # also finish PREPARE and preserve both cheaper signal arms.
                entity_cte = (
                    f"entity_expanded AS (SELECT 'entity' AS source, id FROM {schema}.memory_units "
                    "WHERE id = ANY($1::uuid[]) AND $2::text IS NOT NULL LIMIT $3)"
                )
                fallback_cte = (
                    "semantic_expanded AS (SELECT 'semantic' AS source, NULL::uuid AS id "
                    "WHERE $1::uuid[] IS NOT NULL AND $2::text = 'world' LIMIT $3), "
                    "causal_expanded AS (SELECT 'causal' AS source, NULL::uuid AS id)"
                )
                fallback_ops = SimpleNamespace(
                    build_entity_expansion_cte=lambda *_: entity_cte,
                    build_semantic_causal_cte=lambda *_: fallback_cte,
                )
                rows = await retriever._expand_combined(conn, [], "world", 2, ops=fallback_ops)
                assert rows.entity == []
                assert [r["source"] for r in rows.semantic] == ["semantic"]
                assert [r["source"] for r in rows.causal] == ["causal"]
            assert prepared.is_set()
            assert not conn.is_in_transaction()
        activity = await observer.fetchrow("SELECT state, xact_start FROM pg_stat_activity WHERE pid = $1", pid)
        print(f"AFTER fallback backend={pid}: {dict(activity)}")
        assert dict(activity) == {"state": "idle", "xact_start": None}
        # The cleanup must release the already-blocked production DDL call.
        assert drop is not None
        await asyncio.wait_for(drop, 5)
        assert await observer.fetchval("SELECT to_regclass($1)", f"{schema}.{index}") is None
    finally:
        # Release even the deliberately poisoned baseline backend so RED control
        # failures neither hang pool.close() nor poison subsequent tests.
        async with pool.acquire() as conn:
            await conn.execute("SELECT 1")
        if drop is not None:
            await asyncio.wait_for(drop, 5)
        await pool.close()
        await observer.execute(f"DROP SCHEMA {schema} CASCADE")
        await observer.close()
        await ddl_conn.close()
