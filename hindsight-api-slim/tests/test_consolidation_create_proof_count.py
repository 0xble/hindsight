"""Released distinct-live-source proof counts survive the fork's inline/store routing."""

import uuid
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from hindsight_api.config import _get_raw_config
from hindsight_api.engine.consolidation import consolidator as c


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["native", "vchord", "pg_textsearch", "store_owned"])
async def test_create_proof_count_matches_distinct_surviving_sources(route):
    first, second, third, deleted = [uuid.uuid4() for _ in range(4)]
    candidates = [first, second, first, deleted, third]
    survivors = [first, second, third]
    observation_id = uuid.uuid4()
    conn = SimpleNamespace(
        fetch=AsyncMock(return_value=[{"id": source} for source in survivors]),
        fetchrow=AsyncMock(return_value={"id": observation_id}),
    )
    store = SimpleNamespace(
        store_owned_for=lambda _bank: route == "store_owned",
        get_memories=AsyncMock(return_value=[SimpleNamespace(unit_id=str(source)) for source in survivors]),
        upsert_observation=AsyncMock(),
    )
    engine = SimpleNamespace(_backend=SimpleNamespace(ops=SimpleNamespace(uses_observation_sources_table=False)))
    config = replace(
        _get_raw_config(),
        database_backend="postgresql",
        text_search_extension="native" if route == "store_owned" else route,
    )
    with (
        patch.object(c, "get_memories", return_value=store),
        patch.object(c, "get_config", return_value=config),
        patch.object(c, "_memory_store_handles_tables", return_value=route == "store_owned"),
    ):
        result = await c._apply_create_observation(
            conn=conn,
            memory_engine=engine,
            bank_id="synthetic-proof-count",
            source_memory_ids=candidates,
            observation_text="Three independent sources support this observation.",
            embedding_str="[0.1, 0.2, 0.3]",
        )

    assert result["action"] == "created"
    if route == "store_owned":
        conn.fetchrow.assert_not_awaited()
        store.upsert_observation.assert_awaited_once()
        record = store.upsert_observation.await_args.kwargs["record"]
        assert record.proof_count == 3
        assert record.source_memory_ids == [str(source) for source in survivors]
        assert store.get_memories.await_args.kwargs["bank_id"] == "synthetic-proof-count"
    else:
        store.upsert_observation.assert_not_awaited()
        query, *parameters = conn.fetchrow.await_args.args
        assert "proof_count" in query and "$11" in query
        assert parameters[4] == survivors
        assert parameters[10] == 3
        live_query, live_ids, bank_id = conn.fetch.await_args.args
        assert "ORDER BY id FOR SHARE" in live_query
        assert live_ids == candidates and bank_id == "synthetic-proof-count"
