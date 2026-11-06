"""Fork boundaries on v1.2.0's fact-verdict consolidation protocol."""

import uuid
from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from pydantic import ValidationError

from hindsight_api.config import _get_raw_config
from hindsight_api.engine.consolidation import consolidator as c
from hindsight_api.engine.language_integrity import LanguageCheckResult
from hindsight_api.engine.memories.base import MemoriesExtension
from hindsight_api.engine.response_models import MemoryFact, RecallResult
from tests.test_consolidation_schema_correction import config, install, provider  # noqa: F401


def verdict(**overrides):
    return {
        "fact_id": "F1",
        "kind": "lasting",
        "adds": "yes",
        "existing": [],
        "new": ["Workshop on Tuesday"],
        **overrides,
    }


@pytest.mark.parametrize("invalid", ["invalid sibling", {}, {"fact_id": "F2"}])
def test_malformed_sibling_rejects_complete_verdict(invalid):
    model = c._build_fact_response_model(["F1", "F2"])
    with pytest.raises(ValidationError) as raised:
        model.model_validate({"facts": [verdict(), invalid]})
    assert c._schema_correction_feedback(raised.value, model) is not None


@pytest.mark.parametrize("invalid", ["invalid change", {"observation_id": "O1"}])
def test_malformed_targeted_change_is_not_salvaged(invalid):
    model = c._build_fact_response_model(["F1"])
    with pytest.raises(ValidationError):
        model.model_validate({"facts": [verdict(existing=[invalid])]})


@pytest.mark.parametrize("targetless", [{"effect": "changes", "text": "must not write"}, {"observation_id": ""}])
def test_targetless_change_skip_preserves_valid_fact(targetless):
    model = c._build_fact_response_model(["F1"])
    answer = model.model_validate({"facts": [verdict(existing=[targetless])]})
    assert answer.facts[0].existing == []
    assert answer.facts[0].new == ["Workshop on Tuesday"]


def test_missing_fact_id_is_not_fabricated_by_position():
    model = c._build_fact_response_model(["F1"])
    missing = verdict()
    del missing["fact_id"]
    with pytest.raises(ValidationError):
        model.model_validate({"facts": [missing]})


def test_invalid_update_survives_conversion_until_reference_filter():
    model = c._build_fact_response_model(["F1"])
    answer = model.model_validate(
        {"facts": [verdict(adds="no", existing=[{"observation_id": "UNKNOWN", "effect": "changes", "text": "x"}])]}
    )
    actions = answer.to_actions({}, {"F1": "source"}, lambda *_: False, lambda *_: False)
    assert len(actions.updates) == 1
    filtered = c._filter_unpersistable_references(actions, memories=[{"id": "F1"}], union_observations=[])
    assert filtered.must_reject
    assert filtered.pending_fact_ids == {"F1"}
    assert not filtered.response.updates


def test_momentary_skip_survives_reference_filter():
    response = c._ConsolidationBatchResponse(skips=[c._SkipAction(fact_id="F1")])
    filtered = c._filter_unpersistable_references(response, memories=[{"id": "F1"}], union_observations=[])
    assert filtered.response.skips == response.skips


@pytest.mark.parametrize("skip_id", ["F1", "UNKNOWN"])
def test_skip_cannot_settle_an_invalid_citation_or_unknown_fact(skip_id):
    response = c._ConsolidationBatchResponse(
        updates=[c._UpdateAction(observation_id="UNKNOWN", text="invalid", source_fact_ids=["F1"])],
        skips=[c._SkipAction(fact_id=skip_id)],
    )
    filtered = c._filter_unpersistable_references(response, memories=[{"id": "F1"}], union_observations=[])
    assert filtered.must_reject and filtered.pending_fact_ids == {"F1"}
    assert not filtered.response.skips


def test_all_update_proposals_reach_guard_before_dedup():
    answer = c._FactBatchResponse(
        facts=[
            c._FactDecision(
                fact_id="F1",
                existing=[c._ObservationChange(observation_id="O1", effect="changes", text="lossy")],
            ),
            c._FactDecision(
                fact_id="F2",
                existing=[c._ObservationChange(observation_id="O1", effect="changes", text="complete")],
            ),
        ]
    )
    proposals = []
    actions = answer.to_actions(
        {"O1": "original"},
        {"F1": "first", "F2": "second"},
        lambda *_: False,
        lambda *_: False,
        update_proposals=proposals,
    )
    assert [u.text for u in actions.updates] == ["lossy", "complete"]
    assert actions.updates[0].source_fact_ids == ["F1"]
    assert actions.updates[1].source_fact_ids == ["F1", "F2"]
    final = c._dedupe_updates(actions.updates, batch_label="test")
    assert len(final) == 1
    assert final[0].text == "complete"
    assert final[0].source_fact_ids == ["F1", "F2"]


def test_update_requires_its_own_source_recall_topology():
    response = c._ConsolidationBatchResponse(
        creates=[c._CreateAction(text="valid sibling", source_fact_ids=["F2"])],
        updates=[c._UpdateAction(text="cross-source", observation_id="O1", source_fact_ids=["F1"])],
    )
    filtered = c._filter_unpersistable_references(
        response,
        memories=[{"id": "F1"}, {"id": "F2"}],
        union_observations=[SimpleNamespace(id="O1")],
        per_fact_observation_ids={"F1": set(), "F2": {"O1"}},
    )
    assert not filtered.response.updates
    assert filtered.pending_fact_ids == {"F1"}
    assert filtered.response.creates == response.creates


def test_new_string_sanitization_keeps_released_blank_handling():
    model = c._build_fact_response_model(["F1"])
    answer = model.model_validate({"facts": [verdict(new=[" ", "Workshop on Tuesday", ""])]})
    assert answer.facts[0].new == ["Workshop on Tuesday"]


@pytest.mark.parametrize("invalid", [None, False, 0, {}, []])
def test_invalid_new_sibling_is_not_silently_erased(invalid):
    model = c._build_fact_response_model(["F1"])
    with pytest.raises(ValidationError) as raised:
        model.model_validate({"facts": [verdict(new=["valid sibling", invalid])]})
    assert c._schema_correction_feedback(raised.value, model) is None


@pytest.mark.parametrize(
    "round_size,explicit,limit", [(-1, 10, 0), (0, 10, 0), (99, 10, 0), (100, 10, 1), (1000, 30, 10), (1000, 2, 2)]
)
def test_explicit_correction_allowance_cannot_raise_safety_cap(round_size, explicit, limit):
    budget = c._SchemaCorrectionBudget(round_size, explicit)
    for _ in range(limit):
        budget.start()
    with pytest.raises(c._RoundCorrectionBudgetExhausted):
        budget.start_detail()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["create", "update"])
async def test_store_fold_refusal_is_not_success_or_delete(kind):
    source, target, updated = [str(uuid.uuid4()) for _ in range(3)]
    store = SimpleNamespace(
        fold_sources_into_observation=AsyncMock(return_value=False),
        fold_observation_into_twin=AsyncMock(return_value=False),
        get_memories=AsyncMock(return_value=[]),
        upsert_observation=AsyncMock(),
    )
    outcome = c._DedupOutcome(target, "Workshop on Tuesday", True, "Workshop on Tuesday")
    engine = SimpleNamespace(embeddings=object())
    with (
        patch.object(c, "get_memories", return_value=store),
        patch.object(c, "_memory_store_handles_tables", return_value=True),
        patch.object(c, "_filter_live_source_memories", new=AsyncMock(return_value=[uuid.UUID(source)])),
        patch.object(c, "_execute_delete_action", new=AsyncMock()) as delete,
    ):
        if kind == "create":
            result = await c._apply_dedup_create_fold(
                object(), engine, "bank", outcome, [uuid.UUID(source)], c._TemporalBounds()
            )
            assert result is None
            store.fold_sources_into_observation.assert_awaited_once()
            assert store.fold_sources_into_observation.await_args.kwargs["expected_text"] == outcome.best_text
        else:
            result = await c._apply_dedup_update_fold(
                object(), engine, "bank", _get_raw_config(), outcome, updated, "Workshop on Tuesday"
            )
            assert result is False
            store.fold_observation_into_twin.assert_awaited_once()
        delete.assert_not_awaited()
        store.upsert_observation.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("current", [None, "Workshop on Wednesday"])
async def test_default_store_declines_missing_or_changed_survivor(current):
    store = SimpleNamespace(
        get_memories=AsyncMock(return_value=[] if current is None else [SimpleNamespace(text=current)]),
        upsert_observation=AsyncMock(),
    )
    result = await MemoriesExtension.fold_sources_into_observation(
        store,
        conn=object(),
        fq_table=c.fq_table,
        bank_id="bank",
        observation_id=str(uuid.uuid4()),
        expected_text="Workshop on Tuesday",
        merged_text="Workshop on Tuesday",
        source_memory_ids=[uuid.uuid4()],
        bounds=c._TemporalBounds(),
        embeddings=object(),
    )
    assert result is False
    store.upsert_observation.assert_not_awaited()


@pytest.mark.asyncio
async def test_invalid_new_reply_fails_without_correction_request(provider, config):
    stub = install(provider, [{"facts": [verdict(new=["valid text", None])]}])
    result = await c._consolidate_batch_with_llm(provider, [{"id": "F1", "text": "source"}], [], {}, config)
    assert result.failed and not result.creates
    assert len(stub.requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["update", "create_fold", "update_fold"])
@pytest.mark.parametrize("backend", ["native", "vchord", "pg_textsearch"])
async def test_engine_apply_refreshes_backend_search_vector(operation, backend):
    source, target, updated = [uuid.uuid4() for _ in range(3)]
    cfg = replace(_get_raw_config(), text_search_extension=backend, enable_observation_history=False)
    conn = SimpleNamespace(
        fetchrow=AsyncMock(return_value={"source_memory_ids": [source]}),
        fetchval=AsyncMock(return_value=target),
        execute_rows_affected=AsyncMock(return_value=1),
    )
    store = SimpleNamespace(store_owned_for=lambda _: False, lock_observation_tags=AsyncMock(return_value=[]))
    engine = SimpleNamespace(_backend=SimpleNamespace(ops=SimpleNamespace(uses_observation_sources_table=False)))
    outcome = c._DedupOutcome(str(target), "Workshop on Tuesday", True, "Workshop on Tuesday")
    with (
        patch("hindsight_api.config.get_config", return_value=cfg),
        patch.object(c, "get_config", return_value=cfg),
        patch.object(c, "get_memories", return_value=store),
        patch.object(c, "_memory_store_handles_tables", return_value=False),
        patch.object(c, "_filter_live_source_memories", new=AsyncMock(return_value=[source])),
        patch.object(c, "_execute_delete_action", new=AsyncMock()),
    ):
        if operation == "update":
            prepared = c._PreparedUpdate(
                update=c._UpdateAction(
                    observation_id=str(target), text=outcome.merged_text, source_fact_ids=[str(source)]
                ),
                model=MemoryFact(id=str(target), text="Workshop", fact_type="observation"),
                source_mems=[],
                source_memory_ids=[source],
                source_fact_tags=[],
                source_bounds=c._TemporalBounds(),
                embedding_str="[0.1]",
            )
            await c._apply_update_action(conn, engine, "bank", prepared)
            sql = conn.execute_rows_affected.await_args.args[0]
        elif operation == "create_fold":
            await c._apply_dedup_create_fold(conn, engine, "bank", cfg, outcome, [source], c._TemporalBounds())
            sql = conn.fetchval.await_args.args[0]
        else:
            await c._apply_dedup_update_fold(conn, engine, "bank", cfg, outcome, str(updated), outcome.merged_text)
            sql = conn.fetchval.await_args.args[0]
    if backend == "vchord":
        assert "search_vector = tokenize(COALESCE($1, ''), 'llmlingua2')::bm25_catalog.bm25vector" in sql
    elif backend == "native":
        assert "search_vector = to_tsvector(" in sql
    else:
        assert "search_vector =" not in sql


@pytest.mark.asyncio
async def test_every_corrected_repeated_target_passes_detail_guard(provider, config):
    original = "Timeout is 5 seconds and must remain configured."
    lossy = "Timeout remains configured."
    obs = MemoryFact(id="O1", text=original, fact_type="observation", source_fact_ids=["OLD"])
    old = MemoryFact(id="OLD", text=original, fact_type="world")

    def entry(text):
        return verdict(adds="no", new=[], existing=[{"observation_id": "O1", "effect": "changes", "text": text}])

    stub = install(provider, [{"facts": [entry(lossy)]}, {"facts": [entry(lossy), entry(original)]}])
    result = await c._consolidate_batch_with_llm(provider, [{"id": "F1", "text": lossy}], [obs], {"OLD": old}, config)
    assert not result.failed and len(stub.requests) == 2
    assert not result.updates
    assert len(result.creates) == 1 and result.creates[0]._preserve_separate
    assert result.creates[0].source_fact_ids == ["F1"]


@pytest.mark.asyncio
async def test_corrected_candidates_reach_language_validation_before_selection(provider, config):
    original = "Timeout is 5 seconds and must remain configured."
    lossy = "Timeout remains configured."
    first = original + " First clarification."
    last = original + " Final clarification."
    obs = MemoryFact(id="O1", text=original, fact_type="observation", source_fact_ids=["OLD"])
    old = MemoryFact(id="OLD", text=original, fact_type="world")

    def entry(text):
        return verdict(adds="no", new=[], existing=[{"observation_id": "O1", "effect": "changes", "text": text}])

    stub = install(provider, [{"facts": [entry(lossy)]}, {"facts": [entry(first), entry(last)]}])
    config.llm_language_integrity = "observe"
    evaluate = AsyncMock(return_value=LanguageCheckResult(mismatches=(), checked=2, abstained=0))
    with (
        patch.object(c, "prepare_context_safely", new=AsyncMock(return_value=object())),
        patch.object(c, "evaluate_language_integrity_safely", new=evaluate),
    ):
        result = await c._consolidate_batch_with_llm(
            provider,
            [{"id": "F1", "text": lossy}],
            [obs],
            {"OLD": old},
            config,
            original_source_text_by_id={"F1": lossy, "OLD": original},
        )
    assert not result.failed and len(stub.requests) == 2
    generated = evaluate.await_args.args[1]
    assert [item.text for item in generated] == [first, last]
    assert all(item.source_keys == ("F1", "OLD") for item in generated)
    assert len(result.updates) == 1 and result.updates[0].text == last


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "observation_text,folded", [("stale text", False), ("current text", False), ("current text", True)]
)
async def test_default_update_fold_checks_source_snapshot_and_propagates_refusal(observation_text, folded):
    source = str(uuid.uuid4())
    store = SimpleNamespace(
        get_memories=AsyncMock(
            return_value=[
                SimpleNamespace(
                    text=observation_text,
                    source_memory_ids=[source],
                    event_date=None,
                    occurred_start=None,
                    occurred_end=None,
                    mentioned_at=None,
                )
            ]
        ),
        lock_live_memory_ids=AsyncMock(return_value={source}),
        fold_sources_into_observation=AsyncMock(return_value=folded),
    )
    result = await MemoriesExtension.fold_observation_into_twin(
        store,
        conn=object(),
        fq_table=c.fq_table,
        bank_id="bank",
        observation_id=str(uuid.uuid4()),
        observation_text="current text",
        twin_id=str(uuid.uuid4()),
        twin_text="expected twin",
        merged_text="merged",
        embeddings=object(),
    )
    assert result is (folded if observation_text == "current text" else False)
    if observation_text != "current text":
        store.fold_sources_into_observation.assert_not_awaited()
    else:
        store.fold_sources_into_observation.assert_awaited_once()
        assert store.fold_sources_into_observation.await_args.kwargs["expected_text"] == "expected twin"


@pytest.mark.asyncio
@pytest.mark.parametrize("round_size,request_count", [(0, 2), (99, 2), (100, 3)])
async def test_explicit_budget_cannot_buy_extra_native_provider_completions(
    provider, config, round_size, request_count
):
    from tests.test_consolidation_schema_correction import MEMORIES, MISSING, OBSERVATION, VALID

    budget = c._SchemaCorrectionBudget(round_size, correction_budget=20)
    requests = []
    for index in range(2):
        stub = install(provider, [MISSING, VALID])
        if round_size == 100 and index == 0:
            result = await c._consolidate_batch_with_llm(
                provider, MEMORIES, [OBSERVATION], {}, config, schema_correction_budget=budget
            )
            assert not result.failed
        else:
            with pytest.raises(c._RoundCorrectionBudgetExhausted):
                await c._consolidate_batch_with_llm(
                    provider, MEMORIES, [OBSERVATION], {}, config, schema_correction_budget=budget
                )
        requests.extend(stub.requests)
    assert len(requests) == request_count
    assert budget.stats.attempts == (1 if round_size == 100 else 0)


@pytest.mark.asyncio
@pytest.mark.parametrize("variant,expected_count", [("identical", 1), ("different-text", 2), ("different-target", 2)])
async def test_fallback_coalescing_requires_identical_target_and_text(config, variant, expected_count):
    original = "Timeout is 5 seconds and must remain configured."
    first = c._UpdateAction(observation_id="O1", text="Timeout remains configured.", source_fact_ids=["F1"])
    second = c._UpdateAction(
        observation_id="O2" if variant == "different-target" else "O1",
        text="Timeout stays configured." if variant == "different-text" else first.text,
        source_fact_ids=["F2"],
    )
    observations = [
        MemoryFact(id=oid, text=original, fact_type="observation", source_fact_ids=["OLD"]) for oid in ("O1", "O2")
    ]
    result = await c._guard_detail_loss_updates(
        c._ConsolidationBatchResponse(updates=[first, second]),
        memories=[{"id": "F1", "text": first.text}, {"id": "F2", "text": second.text}],
        observations=observations,
        evidence=c._DetailGuardEvidence(sources={"OLD": c.Evidence(original)}),
        llm_config=None,
        call_kwargs={},
        config=config,
        budget=c._SchemaCorrectionBudget(),
        correction_available=False,
        max_context_tokens=60000,
    )
    assert not result.updates
    assert len(result.creates) == expected_count
    assert all(create._preserve_separate for create in result.creates)
    if expected_count == 1:
        assert result.creates[0].source_fact_ids == ["F1", "F2"]
    else:
        assert [create.source_fact_ids for create in result.creates] == [["F1"], ["F2"]]


@pytest.mark.asyncio
@pytest.mark.memory_backend_incompatible
@pytest.mark.parametrize("store_route", [False, True])
@pytest.mark.parametrize("operation", ["create", "update"])
@pytest.mark.parametrize("state", ["missing", "changed", "unchanged"])
async def test_engine_fold_cas_preserves_rows_and_sources_native(
    memory, request_context, store_route, operation, state
):
    bank = f"verdict-fold-{uuid.uuid4().hex[:8]}"
    source, target, updated = [uuid.uuid4() for _ in range(3)]
    expected = "Workshop on Monday"
    merged = "Workshop on Monday. Workshop on Tuesday"
    early = datetime(2020, 1, 1, tzinfo=timezone.utc)
    late = datetime(2021, 1, 1, tzinfo=timezone.utc)
    cfg = replace(_get_raw_config(), text_search_extension="native")
    await memory.ensure_bank_profile(bank_id=bank, request_context=request_context)
    try:
        async with c.acquire_with_retry(memory._backend) as conn:
            await conn.execute(
                "INSERT INTO memory_units (id, bank_id, text, fact_type, tags) VALUES ($1, $2, 'source', 'world', '{}')",
                source,
                bank,
            )
            if state != "missing":
                await conn.execute(
                    "INSERT INTO memory_units (id, bank_id, text, fact_type, tags, source_memory_ids) VALUES ($1, $2, $3, 'observation', '{}', '{}')",
                    target,
                    bank,
                    "Concurrent rewrite" if state == "changed" else expected,
                )
            if operation == "update":
                await conn.execute(
                    "INSERT INTO memory_units (id, bank_id, text, fact_type, tags, source_memory_ids) VALUES ($1, $2, $3, 'observation', '{}', $4)",
                    updated,
                    bank,
                    merged,
                    [source],
                )
            for unit_id, when in ((target, late), (updated, early)):
                await conn.execute(
                    "UPDATE memory_units SET event_date=$2, occurred_start=$2, occurred_end=$2, mentioned_at=$2 WHERE id=$1",
                    unit_id,
                    when,
                )
            bounds = c._TemporalBounds(event_date=early, occurred_start=early, occurred_end=early, mentioned_at=early)
            outcome = c._DedupOutcome(best_id=str(target), merged_text=merged, should_merge=True, best_text=expected)
            with (
                patch("hindsight_api.config.get_config", return_value=cfg),
                patch.object(c, "get_config", return_value=cfg),
                patch.object(c, "_memory_store_handles_tables", return_value=store_route),
            ):
                async with conn.transaction():
                    if operation == "create":
                        result = await c._apply_dedup_create_fold(conn, memory, bank, cfg, outcome, [source], bounds)
                        assert result == (str(target) if state == "unchanged" else None)
                    else:
                        result = await c._apply_dedup_update_fold(
                            conn, memory, bank, cfg, outcome, str(updated), merged
                        )
                        assert result is (state == "unchanged")
            row = await conn.fetchrow(
                "SELECT text, source_memory_ids, event_date, occurred_start, occurred_end, mentioned_at, search_vector @@ plainto_tsquery('english', 'Tuesday') AS searchable FROM memory_units WHERE id=$1",
                target,
            )
            if state == "missing":
                assert row is None
            elif state == "changed":
                assert row["text"] == "Concurrent rewrite" and row["source_memory_ids"] == []
            else:
                assert row["text"] == merged and row["source_memory_ids"] == [source] and row["searchable"]
                assert row["event_date"] == early and row["occurred_start"] == early
                assert row["occurred_end"] == late and row["mentioned_at"] == late
            if operation == "update":
                remaining = await conn.fetchval("SELECT count(*) FROM memory_units WHERE id=$1", updated)
                assert remaining == (0 if state == "unchanged" else 1)
    finally:
        async with memory._pool.acquire() as conn:
            await conn.execute("DELETE FROM banks WHERE bank_id=$1", bank)


@pytest.mark.asyncio
@pytest.mark.memory_backend_incompatible
@pytest.mark.parametrize("with_create", [False, True])
async def test_filtered_momentary_skip_is_committed_not_retried(memory, request_context, provider, with_create):
    bank = f"verdict-skip-{uuid.uuid4().hex[:8]}"
    ids = [uuid.uuid4() for _ in range(3)]
    memories = [
        {"id": fid, "text": text, "tags": []}
        for fid, text in zip(ids, ["Workshop on Tuesday", "Hello there", "Workshop has moved"], strict=True)
    ]
    await memory.ensure_bank_profile(bank_id=bank, request_context=request_context)
    stub = install(
        provider,
        [
            {
                "facts": ([verdict(fact_id=str(ids[0]))] if with_create else [])
                + [
                    verdict(fact_id=str(ids[1]), kind="momentary", new=[]),
                    verdict(
                        fact_id=str(ids[2]),
                        adds="no",
                        new=[],
                        existing=[{"observation_id": "UNKNOWN", "effect": "changes", "text": "unsupported"}],
                    ),
                ]
            }
        ],
    )
    try:
        async with memory._pool.acquire() as conn:
            for fact in memories:
                await conn.execute(
                    "INSERT INTO memory_units (id, bank_id, text, fact_type, tags) VALUES ($1, $2, $3, 'world', '{}')",
                    fact["id"],
                    bank,
                    fact["text"],
                )
        with (
            patch.object(
                c, "_find_related_observations", new=AsyncMock(return_value=RecallResult(results=[], source_facts={}))
            ),
            patch.object(
                c.embedding_utils,
                "generate_embeddings_batch",
                new=AsyncMock(
                    side_effect=lambda embeddings, texts, **kw: [[0.1] * embeddings.dimension for _ in texts]
                ),
            ),
        ):
            await c._process_memory_batch(
                pool=memory._backend,
                memory_engine=memory,
                llm_config=provider,
                bank_id=bank,
                memories=memories,
                request_context=request_context,
                config=replace(_get_raw_config(), llm_language_integrity="off", consolidation_dedup_threshold=1.0),
                mark_consolidated_ids=ids,
            )
        async with memory._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT id, consolidated_at FROM memory_units WHERE bank_id=$1 AND fact_type='world'", bank
            )
            stamps = {row["id"]: row["consolidated_at"] for row in rows}
            assert (stamps[ids[0]] is not None) is with_create
            assert stamps[ids[1]] is not None
            assert stamps[ids[2]] is None
            observations = await conn.fetch(
                "SELECT source_memory_ids FROM memory_units WHERE bank_id=$1 AND fact_type='observation'", bank
            )
            assert len(observations) == (1 if with_create else 0)
            if with_create:
                assert observations[0]["source_memory_ids"] == [ids[0]]
        assert len(stub.requests) == 1
    finally:
        async with memory._pool.acquire() as conn:
            await conn.execute("DELETE FROM banks WHERE bank_id=$1", bank)
