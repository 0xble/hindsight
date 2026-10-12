"""Graph maintenance's curation exclusion is PostgreSQL-only, not SQL-store-wide."""

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from hindsight_api.engine.memories.pg import graph


@pytest.mark.asyncio
@pytest.mark.parametrize("pass_name", ["relink_pass", "entity_prune_pass"])
@pytest.mark.parametrize(
    "backend_type, lock_available", [("oracle", False), ("postgresql", False), ("postgresql", True)]
)
async def test_graph_maintenance_curation_lock_dialect(pass_name, backend_type, lock_available):
    @asynccontextmanager
    async def transaction():
        yield

    async def advisory_lock(sql, bank_id):
        assert backend_type == "postgresql", "Oracle maintenance must not issue PostgreSQL advisory-lock SQL"
        assert "pg_try_advisory_xact_lock" in sql and "hashtextextended" in sql
        assert bank_id == "dialect-bank"
        return lock_available

    conn = SimpleNamespace(
        backend_type=backend_type, transaction=transaction, fetchval=AsyncMock(side_effect=advisory_lock)
    )

    claim = AsyncMock(return_value=[])
    ops = SimpleNamespace(claim_graph_maintenance_batch=claim, claim_entity_maintenance_batch=claim)
    backend = SimpleNamespace(acquire=AsyncMock(return_value=conn), release=AsyncMock(), ops=ops)
    kwargs = {"backend": backend, "fq_table": lambda table: table, "bank_id": "dialect-bank"}
    if pass_name == "relink_pass":
        kwargs["config"] = None
    result = await getattr(graph, pass_name)(**kwargs)

    if backend_type == "oracle":
        conn.fetchval.assert_not_awaited()
        claim.assert_awaited_once()
        assert result.queue_exhausted
    else:
        conn.fetchval.assert_awaited_once()
        assert result.queue_exhausted == lock_available
        if lock_available:
            claim.assert_awaited_once()
        else:
            claim.assert_not_awaited()
