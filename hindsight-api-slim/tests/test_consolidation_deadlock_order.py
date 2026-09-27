"""Consolidation's deadlock backstop and ordered row-lock contract."""

from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import asyncpg
import pytest

from hindsight_api.engine.consolidation import consolidator
from hindsight_api.engine.db.oracle import _rewrite_pg_to_oracle
from hindsight_api.engine.llm_trace import (
    LLMTraceContext,
    record_created_memory_ids,
    reset_trace_context,
    set_trace_context,
)
from hindsight_api.engine.retain import fact_storage


async def test_apply_retry_restarts_entire_transaction(monkeypatch):
    monkeypatch.setattr(consolidator.asyncio, "sleep", AsyncMock())
    attempts = []
    committed = []
    counters = []
    trace = LLMTraceContext(created_memory_ids=["prior"])
    token = set_trace_context(trace)

    @asynccontextmanager
    async def transaction():
        pending = []
        try:
            yield pending
        except BaseException:
            pending.clear()  # database rollback
            raise
        else:
            committed.extend(pending)

    async def apply():
        counters.clear()  # per-attempt in-memory result accounting
        async with transaction() as pending:
            attempts.append(1)
            pending.append("delete")
            counters.append("delete")
            record_created_memory_ids([f"attempt-{len(attempts)}"])
            if len(attempts) == 1:
                raise asyncpg.DeadlockDetectedError("deadlock detected")
            pending.append("create")
            counters.append("create")

    try:
        await consolidator._retry_deadlocked_apply(apply)
        assert trace.created_memory_ids == ["prior", "attempt-2"]
    finally:
        reset_trace_context(token)
    assert len(attempts) == 2
    assert committed == ["delete", "create"]
    assert counters == ["delete", "create"]


async def test_apply_retry_is_bounded_and_does_not_retry_other_errors(monkeypatch):
    monkeypatch.setattr(consolidator.asyncio, "sleep", AsyncMock())
    apply = AsyncMock(side_effect=asyncpg.DeadlockDetectedError("deadlock"))
    with pytest.raises(asyncpg.DeadlockDetectedError):
        await consolidator._retry_deadlocked_apply(apply)
    assert apply.await_count == consolidator.DEFAULT_MAX_RETRIES + 1

    apply = AsyncMock(side_effect=ValueError("invalid"))
    with pytest.raises(ValueError):
        await consolidator._retry_deadlocked_apply(apply)
    apply.assert_awaited_once()


async def test_source_liveness_acquires_locks_in_id_order(monkeypatch):
    store = MagicMock()
    store.store_owned_for.return_value = False
    monkeypatch.setattr(consolidator, "get_memories", lambda: store)
    conn = MagicMock()
    first, second = uuid4(), uuid4()
    conn.fetch = AsyncMock(return_value=[{"id": first}, {"id": second}])
    assert await consolidator._filter_live_source_memories(conn, "bank", [first, second]) == [first, second]
    sql = conn.fetch.await_args.args[0]
    assert "bank_id = $2 ORDER BY id FOR SHARE" in sql


def test_oracle_rewriter_preserves_ordered_lock():
    query = "SELECT id FROM memory_units WHERE bank_id = $1 ORDER BY id FOR SHARE"
    rewritten = _rewrite_pg_to_oracle(query)
    assert "ORDER BY id FOR UPDATE" in rewritten.query


def test_document_update_prelocks_in_same_order():
    # The same connection is passed by the caller inside its retain transaction.
    source = Path(fact_storage.__file__).read_text()
    assert "WHERE bank_id = $1 AND document_id = $2\n        ORDER BY id FOR UPDATE" in source
