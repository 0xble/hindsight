"""The cached SQL dialect must serialize index DDL across engine event loops."""

import asyncio
import uuid
from unittest.mock import AsyncMock

import pytest

from hindsight_api.engine.db import create_data_access_ops
from hindsight_api.engine.db.ops_postgresql import PostgreSQLOps
from hindsight_api.engine.db_utils import retry_with_backoff


def test_cached_index_ddl_serializes_after_a_contended_loop_closes():
    ops = create_data_access_ops("postgresql")
    schema = f"ddl_loops_{uuid.uuid4().hex}"
    table = f"{schema}.memory_units"
    bank_id = "test-bank"
    internal_id = str(uuid.uuid4())

    async def exercise():
        entered = asyncio.Event()
        finish = asyncio.Event()
        statements = []

        async def execute(sql):
            statements.append(sql)
            if sql.startswith("CREATE INDEX"):
                entered.set()
                await finish.wait()

        conn = AsyncMock(execute=execute)
        create = asyncio.create_task(
            ops.create_bank_vector_indexes(conn, table, bank_id, internal_id, "USING hnsw (embedding)", {"world": "w"})
        )
        await entered.wait()
        drop = asyncio.create_task(ops.drop_bank_vector_indexes(conn, schema, internal_id, {"world": "w"}))
        # Run the drop until its first suspension, forcing lock contention on
        # every loop rather than relying on database/network scheduling.
        await asyncio.sleep(0)
        try:
            assert len(statements) == 1, "Create and drop must share per-table exclusion"
        finally:
            finish.set()
            results = await asyncio.gather(create, drop, return_exceptions=True)
        assert results == [None, None], results
        assert len(statements) == 2
        assert statements[1].startswith("DROP INDEX CONCURRENTLY")

    try:
        asyncio.run(exercise())
        assert create_data_access_ops("postgresql") is ops
        asyncio.run(exercise())
    finally:
        ops._index_ddl_locks.pop(table, None)


@pytest.mark.asyncio
async def test_index_drop_command_timeout_is_not_retried():
    ops = PostgreSQLOps()
    conn = AsyncMock()
    conn.execute.side_effect = TimeoutError
    with pytest.raises(
        RuntimeError, match="Timed out dropping bank vector indexes on timeout_test.memory_units"
    ) as exc:
        await retry_with_backoff(
            lambda: ops.drop_bank_vector_indexes(conn, "timeout_test", "bank-internal-id", {"world": "w"}),
            max_retries=7,
            max_delay=10,
        )
    assert isinstance(exc.value.__cause__, TimeoutError)
    conn.execute.assert_awaited_once()
    assert "bank data deletion may already be committed" in str(exc.value)


@pytest.mark.asyncio
async def test_index_drop_deadline_includes_the_ddl_lock_queue():
    ops = PostgreSQLOps()
    conn = AsyncMock()
    async with ops._index_ddl_lock("timeout_test.memory_units"):
        with pytest.raises(RuntimeError, match="after 0.01s"):
            await ops.drop_bank_vector_indexes(
                conn, "timeout_test", "bank-internal-id", {"world": "w"}, command_timeout=0.01
            )
    conn.execute.assert_not_awaited()
    # A timed-out waiter must not consume or leak the lock.
    await ops.drop_bank_vector_indexes(conn, "timeout_test", "bank-internal-id", {"world": "w"})
    conn.execute.assert_awaited_once()


@pytest.mark.asyncio
async def test_index_drop_uses_effective_engine_budget(monkeypatch):
    from types import SimpleNamespace

    from hindsight_api.engine.db import ops_postgresql
    from hindsight_api.engine.retain import bank_utils

    monkeypatch.setattr(ops_postgresql, "get_config", lambda: SimpleNamespace(db_command_timeout=0.001))
    ops = PostgreSQLOps()

    async def execute(sql):
        await asyncio.sleep(0.005)

    conn = AsyncMock(execute=execute)
    # Engine -> bank helper -> dialect must preserve a per-engine override,
    # rather than cutting it short using the process-global default.
    await bank_utils.drop_bank_vector_indexes(conn, "bank-internal-id", ops=ops, command_timeout=1)


@pytest.mark.asyncio
async def test_index_drop_keeps_cancellation_and_deadlock_semantics():
    from asyncpg import DeadlockDetectedError

    ops = PostgreSQLOps()
    conn = AsyncMock()
    for error in (asyncio.CancelledError, DeadlockDetectedError):
        conn.execute.side_effect = error
        with pytest.raises(error):
            await ops.drop_bank_vector_indexes(conn, "timeout_test", "bank-internal-id", {"world": "w"})
    conn.execute.side_effect = None
    await ops.drop_bank_vector_indexes(conn, "timeout_test", "bank-internal-id", {"world": "w"})
