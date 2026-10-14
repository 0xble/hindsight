"""Co-occurrence lookup cost during trigram entity resolution (upstream #5368).

Resolution needs two facts per candidate: its co-occurrence degree (how many distinct
entities it has been seen with) and which *other candidates* it co-occurs with. Both
used to be derived client-side from every edge touching any candidate, so a hub entity
with tens of thousands of partners shipped tens of thousands of rows into Python on
every retain, decoded on the event-loop thread. These tests pin that the same answers
come back while only candidate-to-candidate edges cross the wire.
"""

import uuid
from datetime import UTC, datetime

import pytest

from hindsight_api.engine.db import create_database_backend
from hindsight_api.engine.memories.pg.entity_resolver import (
    EntityResolver,
    _build_cooccurrence_index,
    _cooccurrence_index_from_aggregates,
)
from hindsight_api.pg0 import resolve_database_url


def test_aggregate_index_matches_edge_index():
    """Degrees + candidate-pair edges reproduce what the full edge scan produced."""
    edges = [
        ("c1", "c2"),
        ("c1", "c3"),
        ("c1", "x1"),
        ("c1", "x2"),
        ("c1", "x3"),
        ("c2", "c3"),
        ("c2", "x2"),
        ("c2", "x4"),
        ("c3", "x3"),
        ("c3", "x5"),
        ("c4", "x1"),
        ("c4", "x2"),
        ("c4", "x3"),
        ("x1", "x2"),
    ]
    id_to_name = {"c1": "alpha", "c2": "beta", "c3": "gamma", "c4": "delta"}
    touching = [{"entity_id_1": a, "entity_id_2": b} for a, b in edges if a in id_to_name or b in id_to_name]
    expected = _build_cooccurrence_index(touching, id_to_name)

    degrees = {}
    for a, b in edges:
        for e in (a, b):
            if e in id_to_name:
                degrees[e] = degrees.get(e, 0) + 1
    pair_rows = [{"entity_id_1": a, "entity_id_2": b} for a, b in edges if a in id_to_name and b in id_to_name]
    actual = _cooccurrence_index_from_aggregates(
        [{"id": k, "degree": v} for k, v in degrees.items()], pair_rows, id_to_name
    )

    assert actual.degree_by_name == expected.degree_by_name
    for entity_id in id_to_name:
        assert actual.by_entity.get(entity_id, set()) == expected.by_entity.get(entity_id, set())


def test_aggregate_index_keeps_larger_degree_for_duplicate_names():
    """Two ids sharing a name (mid-merge) keep the larger degree, as before."""
    index = _cooccurrence_index_from_aggregates(
        [{"id": "a", "degree": 3}, {"id": "b", "degree": 7}], [], {"a": "same", "b": "same", "c": "lonely"}
    )
    assert index.degree_by_name == {"same": 7, "lonely": 0}


@pytest.mark.asyncio
async def test_trigram_resolution_does_not_fetch_hub_partner_edges(pg0_db_url):
    """A hub's partners outside the candidate set must not be shipped to the client,
    yet its degree must still count them."""
    resolved_url = await resolve_database_url(pg0_db_url)
    backend = create_database_backend("postgresql")
    await backend.initialize(resolved_url, min_size=1, max_size=2, command_timeout=30)
    bank_id = f"test-cooc-agg-{uuid.uuid4().hex[:8]}"
    resolver = EntityResolver(pool=backend, entity_lookup="trigram")
    now = datetime.now(UTC)
    partner_count = 300

    try:
        async with backend.acquire() as conn:
            hub = await conn.fetchval(
                "INSERT INTO entities (bank_id, canonical_name, first_seen, last_seen, mention_count) "
                "VALUES ($1, 'Hubert Hubbard', $2, $2, 50) RETURNING id",
                bank_id,
                now,
            )
            peer = await conn.fetchval(
                "INSERT INTO entities (bank_id, canonical_name, first_seen, last_seen, mention_count) "
                "VALUES ($1, 'Petra Peerson', $2, $2, 5) RETURNING id",
                bank_id,
                now,
            )
            partners = await conn.fetch(
                "INSERT INTO entities (bank_id, canonical_name, first_seen, last_seen, mention_count) "
                "SELECT $1, 'zz partner ' || g, $2, $2, 1 FROM generate_series(1, $3) g RETURNING id",
                bank_id,
                now,
                partner_count,
            )
            pairs = [(hub, peer)] + [(hub, r["id"]) for r in partners]
            await conn.executemany(
                "INSERT INTO entity_cooccurrences (entity_id_1, entity_id_2, cooccurrence_count, last_cooccurred) "
                "VALUES (LEAST($1::uuid, $2::uuid), GREATEST($1::uuid, $2::uuid), 1, now())",
                pairs,
            )

            fetched_rows: list[int] = []

            class _RecordingConn:
                """asyncpg connections are not monkeypatchable; record through a proxy."""

                def __getattr__(self, name):
                    return getattr(conn, name)

                async def fetch(self, query, *args, **kwargs):
                    rows = await conn.fetch(query, *args, **kwargs)
                    if "entity_cooccurrences" in query:
                        fetched_rows.append(len(rows))
                    return rows

            captured: dict = {}

            async def capture(_conn, _bank, _data, _date, _cands, cooc_map, _tax, _labels, degrees):
                captured["map"], captured["degrees"] = cooc_map, degrees
                return []

            resolver._resolve_from_candidates = capture  # type: ignore[method-assign]
            await resolver._resolve_entities_batch_trigram(
                conn=_RecordingConn(),
                bank_id=bank_id,
                entities_data=[
                    {"text": "Hubert Hubbard", "nearby_entities": [], "event_date": None},
                    {"text": "Petra Peerson", "nearby_entities": [], "event_date": None},
                ],
                unit_event_date=None,
            )

        assert captured["degrees"]["hubert hubbard"] == partner_count + 1
        assert captured["degrees"]["petra peerson"] == 1
        assert captured["map"][hub] == {"petra peerson"}
        assert captured["map"][peer] == {"hubert hubbard"}
        # Only per-candidate aggregates and candidate-to-candidate edges cross the wire.
        assert sum(fetched_rows) <= 3, f"fetched {fetched_rows} co-occurrence rows"
    finally:
        async with backend.acquire() as conn:
            await conn.execute(
                "DELETE FROM entity_cooccurrences WHERE entity_id_1 IN (SELECT id FROM entities WHERE bank_id = $1) "
                "OR entity_id_2 IN (SELECT id FROM entities WHERE bank_id = $1)",
                bank_id,
            )
            await conn.execute("DELETE FROM entities WHERE bank_id = $1", bank_id)
        await backend.shutdown()
