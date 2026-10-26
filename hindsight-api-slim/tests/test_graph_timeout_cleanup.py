"""Graph deadlines must not leave an implicit asyncpg transaction in the pool."""

import asyncio
from contextlib import suppress
from types import SimpleNamespace
import asyncpg
import pytest

from hindsight_api.engine.db.ops import LinkExpansionRows
from hindsight_api.engine.db.postgresql import _NoResetConnection
from hindsight_api.engine.memories.pg import link_expansion
from hindsight_api.engine.memories.pg.link_expansion import LinkExpansionRetriever


@pytest.mark.asyncio
async def test_observation_timeout_after_prepare_leaves_no_server_transaction(pg0_db_url, monkeypatch):
    # The production pool deliberately sends no release-time reset SQL. Parse /
    # Describe / Flush opens an implicit transaction, but asyncpg does not learn
    # its state until Sync. Stop precisely after PREPARE succeeds and before the
    # fetch resumes, reproducing the narrow deadline window without wall-clock
    # races or a slow graph query.
    pool = await asyncpg.create_pool(
        pg0_db_url, min_size=1, max_size=1, statement_cache_size=0, connection_class=_NoResetConnection
    )
    observer = await asyncpg.connect(pg0_db_url)
    prepared = asyncio.Event()
    original_get_statement = asyncpg.Connection._get_statement
    raw_connection = None

    async def pause_after_prepare(self, *args, **kwargs):
        statement = await original_get_statement(self, *args, **kwargs)
        if self is raw_connection:
            prepared.set()
            await asyncio.Future()
        return statement

    async def deadline_after_prepare(coro, *, timeout):
        task = asyncio.create_task(coro)
        try:
            await prepared.wait()
        finally:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        raise TimeoutError

    async def expand(conn, *_args):
        # A real extended-query prepare against the shared relation, not a fake
        # sleeping coroutine. Its unfinished transaction blocks concurrent index
        # deletion even though the driver's is_in_transaction() is still false.
        return await conn.fetch("SELECT id FROM public.memory_units WHERE id = $1::uuid", None)

    monkeypatch.setattr(asyncpg.Connection, "_get_statement", pause_after_prepare)
    monkeypatch.setattr(
        link_expansion, "asyncio", SimpleNamespace(wait_for=deadline_after_prepare, TimeoutError=TimeoutError)
    )
    try:
        async with pool.acquire() as conn:
            raw_connection = conn._con
            pid = conn.get_server_pid()
            rows = await LinkExpansionRetriever()._expand_observations(
                conn, [], 2, ops=SimpleNamespace(expand_observations=expand)
            )
            assert rows == LinkExpansionRows(entity=[], semantic=[], causal=[])
            assert prepared.is_set()
            assert not conn.is_in_transaction()
        activity = await observer.fetchrow("SELECT state, xact_start FROM pg_stat_activity WHERE pid = $1", pid)
        assert dict(activity) == {"state": "idle", "xact_start": None}
    finally:
        # Also release the deliberately poisoned baseline connection when this
        # test demonstrates the bug, so failure does not stall later tests.
        async with pool.acquire() as conn:
            await conn.execute("SELECT 1")
        await pool.close()
        await observer.close()
