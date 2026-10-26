"""The cached SQL dialect must serialize index DDL across engine event loops."""

import asyncio
import uuid
from unittest.mock import AsyncMock

from hindsight_api.engine.db import create_data_access_ops


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
