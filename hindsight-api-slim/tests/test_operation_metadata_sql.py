"""Failure/retry metadata SQL from the engine must survive the Oracle adapter."""

import json
import re
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import asyncpg
import pytest
import pytest_asyncio

from hindsight_api import MemoryEngine, RequestContext
from hindsight_api.engine import memory_engine as engine_module
from hindsight_api.engine.db.oracle import _rewrite_pg_to_oracle


@dataclass
class OperationHarness:
    engine: MemoryEngine
    conn: Any


@pytest.fixture
def captured_engine(monkeypatch):
    """Run real engine methods; replace only connection I/O and parent rollup."""
    conn = MagicMock()
    conn.fetchrow = AsyncMock(return_value={"operation_id": uuid.uuid4()})
    transaction = MagicMock()
    transaction.__aenter__ = AsyncMock()
    transaction.__aexit__ = AsyncMock(return_value=False)
    conn.transaction.return_value = transaction

    @asynccontextmanager
    async def acquire(backend):
        yield conn

    monkeypatch.setattr(engine_module, "acquire_with_retry", acquire)
    engine = object.__new__(MemoryEngine)
    engine._get_backend = AsyncMock()
    engine._authenticate_tenant = AsyncMock()
    engine._operation_validator = None
    engine._maybe_update_parent_operation = AsyncMock()
    return OperationHarness(engine, conn)


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["failure", "retry"])
async def test_operation_metadata_query_survives_oracle_rewrite(captured_engine, monkeypatch, action):
    monkeypatch.setenv("HINDSIGHT_API_DATABASE_BACKEND", "oracle")
    engine = captured_engine.engine
    conn = captured_engine.conn
    operation_id = str(uuid.uuid4())
    if action == "failure":
        await engine._mark_operation_failed(
            operation_id, "parser crashed", "traceback", result_metadata={"failure_class": "no_extractable_text"}
        )
    else:
        result = await engine.retry_operation("bank", operation_id, request_context=RequestContext())
        assert result["success"] is True

    conn.fetchrow.assert_awaited_once()
    query, *params = conn.fetchrow.await_args.args
    translated = _rewrite_pg_to_oracle(query)
    sql = " ".join(translated.query.split())
    # Inspect the entire actual statement, not a hand-written approximation.
    assert "||" not in sql, sql
    assert "::" not in sql
    assert "$" not in sql
    assert "JSON_MERGEPATCH(" in sql
    assert "TO_CLOB('{}')" in sql
    assert "CASE WHEN operation_type = 'file_convert_retain'" in sql
    assert translated.returning_cols == ["operation_id"]
    assert set(re.findall(r":(\d+)\b", sql)) == {str(i) for i in range(1, len(params) + 1)}
    assert json.loads(params[2]) == {"failure_class": None, "failure_reason": None, "parsers": None}
    if action == "failure":
        assert sql.count("JSON_MERGEPATCH(") == 2
        assert sql.count("RETURNING CLOB") == 2
        assert (
            "JSON_MERGEPATCH(CASE WHEN operation_type = 'file_convert_retain' "
            "THEN JSON_MERGEPATCH(COALESCE(result_metadata, TO_CLOB('{}')), :3 RETURNING CLOB) "
            "ELSE COALESCE(result_metadata, TO_CLOB('{}')) END, :4 RETURNING CLOB)"
        ) in sql
        assert "status NOT IN ('completed', 'failed', 'cancelled')" in sql
        assert json.loads(params[3]) == {"failure_class": "no_extractable_text"}
        conn.transaction.assert_called_once()
        engine._maybe_update_parent_operation.assert_awaited_once_with(operation_id, conn)
    else:
        assert sql.count("JSON_MERGEPATCH(") == 1
        assert sql.count("RETURNING CLOB") == 1
        assert (
            "CASE WHEN operation_type = 'file_convert_retain' "
            "THEN JSON_MERGEPATCH(COALESCE(result_metadata, TO_CLOB('{}')), :3 RETURNING CLOB) "
            "ELSE COALESCE(result_metadata, TO_CLOB('{}')) END"
        ) in sql
        assert "bank_id = :2" in sql
        assert "status IN ('failed', 'cancelled')" in sql
        assert "worker_id = NULL" in sql
        assert "completed_at = NULL" in sql


@pytest.mark.asyncio
async def test_terminal_failure_does_not_roll_up_parent(captured_engine, monkeypatch):
    monkeypatch.setenv("HINDSIGHT_API_DATABASE_BACKEND", "oracle")
    engine = captured_engine.engine
    conn = captured_engine.conn
    conn.fetchrow.return_value = None
    await engine._mark_operation_failed(str(uuid.uuid4()), "late error", "traceback")
    engine._maybe_update_parent_operation.assert_not_awaited()


@pytest_asyncio.fixture
async def pg_operation_engine(captured_engine, pg0_db_url, monkeypatch):
    """Use the migrated operation schema on an isolated temporary table."""
    monkeypatch.setenv("HINDSIGHT_API_DATABASE_BACKEND", "postgresql")
    monkeypatch.setattr(engine_module, "get_current_schema", lambda: "pg_temp")
    conn = await asyncpg.connect(pg0_db_url)
    try:
        await conn.execute("CREATE TEMP TABLE async_operations (LIKE public.async_operations INCLUDING ALL)")

        @asynccontextmanager
        async def acquire(backend):
            yield conn

        monkeypatch.setattr(engine_module, "acquire_with_retry", acquire)
        yield OperationHarness(captured_engine.engine, conn)
    finally:
        await conn.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["failure", "retry"])
@pytest.mark.parametrize("operation_type", ["file_convert_retain", "retain"])
@pytest.mark.parametrize("initial_metadata", [{}, {"source": "fixture", "nested": {"keep": 1}}])
async def test_postgres_metadata_transition_preserves_unrelated_keys(
    pg_operation_engine, action, operation_type, initial_metadata
):
    engine = pg_operation_engine.engine
    conn = pg_operation_engine.conn
    operation_id = uuid.uuid4()
    metadata = (
        {
            **initial_metadata,
            "failure_class": "low_quality_ocr",
            "failure_reason": "refusal_or_no_text_response",
            "parsers": ["iris"],
        }
        if initial_metadata
        else {}
    )
    await conn.execute(
        "INSERT INTO async_operations (operation_id, bank_id, operation_type, status, task_payload, result_metadata) "
        "VALUES ($1, 'bank', $2, $3, '{}'::jsonb, $4::jsonb)",
        operation_id,
        operation_type,
        "processing" if action == "failure" else "failed",
        json.dumps(metadata),
    )
    new_metadata = {"attempt": 2, "nested": {"replace": 2}, "failure_reason": "empty_content"}
    if action == "failure":
        await engine._mark_operation_failed(str(operation_id), "error", "traceback", result_metadata=new_metadata)
    else:
        await engine.retry_operation("bank", str(operation_id), request_context=RequestContext())

    row = await conn.fetchrow("SELECT * FROM async_operations WHERE operation_id = $1", operation_id)
    expected = metadata or {}
    if operation_type == "file_convert_retain":
        expected = {**expected, "failure_class": None, "failure_reason": None, "parsers": None}
    if action == "failure":
        expected = {**expected, **new_metadata}
    assert json.loads(row["result_metadata"]) == expected
    assert row["status"] == ("failed" if action == "failure" else "pending")
    assert (row["completed_at"] is not None) == (action == "failure")


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["completed", "failed", "cancelled"])
async def test_postgres_failure_keeps_terminal_status_and_metadata(pg_operation_engine, status):
    engine = pg_operation_engine.engine
    conn = pg_operation_engine.conn
    operation_id = uuid.uuid4()
    metadata = {"source": "unchanged", "failure_class": "low_quality_ocr"}
    await conn.execute(
        "INSERT INTO async_operations (operation_id, bank_id, operation_type, status, result_metadata) "
        "VALUES ($1, 'bank', 'file_convert_retain', $2, $3::jsonb)",
        operation_id,
        status,
        json.dumps(metadata),
    )
    await engine._mark_operation_failed(str(operation_id), "late error", "traceback", result_metadata={"new": True})
    row = await conn.fetchrow("SELECT * FROM async_operations WHERE operation_id = $1", operation_id)
    assert row["status"] == status
    assert json.loads(row["result_metadata"]) == metadata
    engine._maybe_update_parent_operation.assert_not_awaited()


@pytest.mark.asyncio
async def test_postgres_failure_rolls_back_metadata_with_parent_update(pg_operation_engine):
    engine = pg_operation_engine.engine
    conn = pg_operation_engine.conn
    operation_id = uuid.uuid4()
    metadata = {"source": "unchanged", "failure_class": "low_quality_ocr"}
    await conn.execute(
        "INSERT INTO async_operations (operation_id, bank_id, operation_type, status, result_metadata) "
        "VALUES ($1, 'bank', 'file_convert_retain', 'processing', $2::jsonb)",
        operation_id,
        json.dumps(metadata),
    )
    engine._maybe_update_parent_operation.side_effect = RuntimeError("rollup failed")
    await engine._mark_operation_failed(str(operation_id), "error", "traceback", result_metadata={"new": True})
    row = await conn.fetchrow("SELECT * FROM async_operations WHERE operation_id = $1", operation_id)
    assert row["status"] == "processing"
    assert row["completed_at"] is None
    assert row["error_message"] is None
    assert json.loads(row["result_metadata"]) == metadata
