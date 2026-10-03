"""Synthetic provider responses exercise correction mechanics, not live model efficacy."""

import asyncio
import copy
import json
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from pydantic import BaseModel, ValidationError

from hindsight_api.engine.consolidation import consolidator as c
from hindsight_api.engine.llm_interface import OutputTooLongError, ProviderRateLimitResetError
from hindsight_api.engine.llm_wrapper import LLMProvider

F = "11111111-1111-4111-8111-111111111111"
OBS_ID = "33333333-3333-4333-8333-333333333333"
UNKNOWN = "44444444-4444-4444-8444-444444444444"
MEMORIES = [{"id": F, "text": "Synthetic workshop moved to Tuesday."}]
OBSERVATION = SimpleNamespace(
    id=OBS_ID,
    text="Workshop on Monday",
    proof_count=1,
    occurred_start=None,
    occurred_end=None,
    mentioned_at=None,
    source_fact_ids=[],
    tags=[],
)
VALID = {"updates": [{"text": "Workshop moved to Tuesday", "observation_id": OBS_ID, "source_fact_ids": [F]}]}
MISSING = {"updates": [{"reason": "PRIVATE_MALFORMED_VALUE"}]}
STRINGS = {"updates": ["PRIVATE_MALFORMED_VALUE"]}


@pytest.fixture
def config():
    return SimpleNamespace(
        llm_supports_max_items=True,
        consolidation_max_context_tokens=60000,
        llm_temperature_consolidation=0.0,
        llm_strict_schema_consolidation=False,
        consolidation_max_attempts=3,
        consolidation_llm_max_retries=2,
        consolidation_max_completion_tokens=4000,
        llm_output_language=None,
        observations_mission=None,
        llm_language_integrity="off",
    )


class SDKStub:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    async def create(self, **kwargs):
        self.requests.append(copy.deepcopy(kwargs))
        value = self.responses[min(len(self.requests) - 1, len(self.responses) - 1)]
        if isinstance(value, BaseException):
            raise value
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(value)), finish_reason="stop")],
            usage=SimpleNamespace(
                prompt_tokens=10,
                completion_tokens=12,
                total_tokens=22,
                prompt_tokens_details=None,
                completion_tokens_details=None,
            ),
        )


@pytest.fixture
async def provider():
    llm = LLMProvider(
        provider="openai",
        api_key="test-key",
        base_url="https://example.invalid/v1",
        model="synthetic-model",
    )
    await llm._provider_impl._client.close()
    yield llm


def install(llm, responses):
    stub = SDKStub(responses)
    llm._provider_impl._client = SimpleNamespace(chat=SimpleNamespace(completions=stub))
    return stub


async def batch(llm, config, **kwargs):
    return await c._consolidate_batch_with_llm(llm, MEMORIES, [OBSERVATION], {}, config, **kwargs)


@pytest.mark.asyncio
@pytest.mark.parametrize("malformed", [MISSING, STRINGS, {"creates": ["PRIVATE_MALFORMED_VALUE"]}, {"deletes": [{}]}])
async def test_corrects_whole_response_once(provider, config, malformed, caplog):
    stub = install(provider, [malformed, VALID])
    result = await batch(provider, config)
    assert not result.failed
    assert result.failed_attempts == 1
    assert len(stub.requests) == 2
    first, second = stub.requests
    assert first["model"] == second["model"] == "synthetic-model"
    assert first["max_tokens"] == second["max_tokens"] == 4000
    assert first["temperature"] == second["temperature"] == 0.0
    assert first["response_format"] == second["response_format"]
    assert first["messages"] != second["messages"]
    feedback = second["messages"][-1]["content"]
    assert "COMPLETE" in feedback
    assert "PRIVATE_MALFORMED_VALUE" not in feedback
    assert "PRIVATE_MALFORMED_VALUE" not in caplog.text
    assert result.updates[0].source_fact_ids == [F]


@pytest.mark.asyncio
async def test_persistent_malformation_does_not_consume_third_response(provider, config):
    stub = install(provider, [MISSING, MISSING, VALID])
    result = await batch(provider, config)
    assert result.failed and result.failed_attempts == 2
    assert len(stub.requests) == 2


@pytest.mark.asyncio
async def test_valid_first_adds_no_requests(provider, config):
    stub = install(provider, [VALID])
    result = await batch(provider, config)
    assert not result.failed and result.failed_attempts == 0
    assert len(stub.requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("key", ["source_fact_ids", "observation_id"])
async def test_corrected_references_still_fail_closed(provider, config, key):
    invalid = copy.deepcopy(VALID)
    invalid["updates"][0][key] = [UNKNOWN] if key == "source_fact_ids" else UNKNOWN
    stub = install(provider, [MISSING, invalid])
    result = await batch(provider, config)
    assert result.failed and not result.updates
    assert len(stub.requests) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "invalid", [{"updates": {}}, {"updates": [{"text": "x", "observation_id": 3, "source_fact_ids": [F]}]}]
)
async def test_non_whitelisted_schema_errors_fail_fast(provider, config, invalid):
    stub = install(provider, [invalid, VALID])
    result = await batch(provider, config)
    assert result.failed and len(stub.requests) == 1


@pytest.mark.asyncio
async def test_generic_model_validation_remains_fail_fast(config):
    class OtherModel(BaseModel):
        updates: list[int]

    with pytest.raises(ValidationError) as raised:
        OtherModel.model_validate({"updates": ["bad"]})
    llm = SimpleNamespace(_provider_impl=None, call=AsyncMock(side_effect=raised.value))
    result = await batch(llm, config)
    assert result.failed and llm.call.call_count == 1


@pytest.mark.asyncio
async def test_context_guard_includes_schema_and_feedback(provider, config):
    stub = install(provider, [MISSING, VALID])
    # The initial base prompt fits; the correction's schema-inclusive prompt does not.
    with patch.object(c, "count_tokens", side_effect=lambda text: 100 if "COMPLETE" in text else 1):
        config.consolidation_max_context_tokens = 10
        result = await batch(provider, config)
    assert result.failed and len(stub.requests) == 1


@pytest.mark.asyncio
async def test_concurrent_round_budget_caps_extra_completions(provider, config):
    budget = c._SchemaCorrectionBudget()

    # Alternate per-call local malformed/valid outputs without sharing a response queue.
    async def one():
        llm = LLMProvider(provider="openai", api_key="test-key", base_url="https://example.invalid", model="stub")
        await llm._provider_impl._client.close()
        stub = install(llm, [MISSING, VALID])
        result = await batch(llm, config, schema_correction_budget=budget)
        return len(stub.requests), result.failed

    results = await asyncio.gather(*(one() for _ in range(24)))
    assert sum(count - 1 for count, _ in results) == 10
    assert sum(failed for _, failed in results) == 14
    assert budget.stats.attempts == 10
    assert budget.stats.budget_exhausted == 14


@pytest.mark.parametrize(
    "round_size,credits",
    [(-1, 0), (0, 0), (99, 0), (100, 1), (199, 1), (500, 5), (999, 9), (1000, 10), (2000, 10)],
)
def test_round_budget_scales_down_without_rounding_up(round_size, credits):
    from hindsight_api.engine.llm_attempt_limit import CompletionAttemptLimitError

    budget = c._SchemaCorrectionBudget(round_size)
    for _ in range(credits):
        budget.start()
    with pytest.raises(CompletionAttemptLimitError):
        budget.start()
    assert budget.stats.attempts == credits
    assert budget.stats.budget_exhausted == 1


@pytest.mark.asyncio
async def test_default_size_requeued_rounds_share_per_1000_fact_bound(provider, config, monkeypatch):
    """Exercise real round/requeue and provider guards with in-memory store/apply seams."""
    import uuid
    from contextlib import asynccontextmanager

    from hindsight_api.config import DEFAULT_CONSOLIDATION_MAX_MEMORIES_PER_ROUND

    config.enable_observations = True
    config.consolidation_batch_size = 100
    config.consolidation_max_memories_per_round = DEFAULT_CONSOLIDATION_MAX_MEMORIES_PER_ROUND
    config.consolidation_llm_batch_size = 10
    config.consolidation_llm_parallelism = 1
    config.consolidation_lane_llm_parallelism = 1
    assert config.consolidation_max_memories_per_round == 100
    pending = [{"id": uuid.UUID(int=index + 1), "text": f"Synthetic fact {index}", "tags": []} for index in range(1000)]
    queued = [{"bank_id": "synthetic-budget-bank", "request_context": SimpleNamespace()}]
    requests = []
    conn = SimpleNamespace(fetchrow=AsyncMock(return_value={"bank_id": "synthetic-budget-bank", "name": "Test"}))

    @asynccontextmanager
    async def transaction():
        yield

    @asynccontextmanager
    async def acquire(pool):
        assert pool is engine._backend
        yield conn

    conn.transaction = transaction

    async def fetch(conn, bank_id, fact_types, limit, scopes, deferred, **kwargs):
        return list(pending[:limit])

    async def count(*args, **kwargs):
        return len(pending)

    def remove(ids):
        ids = {str(mid) for mid in ids}
        pending[:] = [memory for memory in pending if str(memory["id"]) not in ids]

    async def mark_failed(**kwargs):
        assert kwargs["failed"]
        remove(kwargs["unit_ids"])

    async def process(**kwargs):
        stub = install(provider, [MISSING, {}])
        result = await c._consolidate_batch_with_llm(
            provider, kwargs["memories"], [], {}, config, schema_correction_budget=kwargs["schema_correction_budget"]
        )
        requests.extend(stub.requests)
        if result.failed:
            return [], 0, True
        remove(memory["id"] for memory in kwargs["memories"])
        return [{"action": "skipped"} for _ in kwargs["memories"]], 0, False

    async def requeue(**payload):
        queued.append(payload)

    engine = SimpleNamespace(
        _backend=object(),
        _write_operation_progress=AsyncMock(),
        submit_async_consolidation=AsyncMock(side_effect=requeue),
    )
    monkeypatch.setattr(c, "acquire_with_retry", acquire)
    monkeypatch.setattr(c, "_fetch_unconsolidated_rows", fetch)
    monkeypatch.setattr(c, "_count_unconsolidated_rows", count)
    monkeypatch.setattr(c, "_effective_lane_parallelism", lambda *args: 1)
    monkeypatch.setattr(c, "_process_memory_batch", process)
    monkeypatch.setattr(c, "get_memories", lambda: SimpleNamespace(mark_consolidated=mark_failed))
    monkeypatch.setattr(c, "_trigger_mental_model_refreshes", AsyncMock(return_value=0))
    results = []
    while queued:
        results.append(
            await c._run_consolidation_job(memory_engine=engine, config=config, llm_config=provider, **queued.pop(0))
        )
    processed_rounds = [result for result in results if result["memories_processed"]]
    assert len(processed_rounds) == 10
    assert all(result["schema_correction_attempts"] == 1 for result in processed_rounds)
    assert sum(result["memories_processed"] for result in processed_rounds) == 1000
    assert sum("Return a COMPLETE replacement" in request["messages"][-1]["content"] for request in requests) == 10
    assert engine.submit_async_consolidation.await_count == 10
    assert not pending


class AuthError(Exception):
    def __init__(self, status_code):
        self.status_code = status_code
        super().__init__(f"HTTP {status_code}")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [
        AuthError(401),
        AuthError(403),
        ProviderRateLimitResetError(retry_at=datetime(2030, 1, 1, tzinfo=timezone.utc), message="quota"),
        asyncio.CancelledError(),
    ],
)
async def test_correction_control_signals_propagate_exactly(provider, config, error):
    stub = install(provider, [MISSING, error, VALID])
    with pytest.raises(type(error)) as raised:
        await batch(provider, config)
    assert raised.value is error
    assert len(stub.requests) == 2


@pytest.mark.asyncio
async def test_correction_transport_failure_has_no_outer_or_inner_retry(provider, config):
    stub = install(provider, [MISSING, RuntimeError("synthetic transport failure"), VALID])
    result = await batch(provider, config)
    assert result.failed and len(stub.requests) == 2


@pytest.mark.asyncio
async def test_output_limit_stays_fail_fast(config):
    llm = SimpleNamespace(_provider_impl=None, call=AsyncMock(side_effect=OutputTooLongError("limit")))
    result = await batch(llm, config)
    assert result.failed and llm.call.call_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 403])
@pytest.mark.parametrize("cancel_refresh", [False, True])
async def test_codex_hidden_auth_retry_cannot_make_second_correction(provider, config, status, cancel_refresh):
    from hindsight_api.engine.aiohttp_session import UpstreamHTTPError
    from hindsight_api.engine.providers.codex_llm import CodexLLM
    from tests.codex_stream_stub import CodexReply, stub_codex_stream_with

    with (
        patch.object(CodexLLM, "_load_codex_auth", return_value=("synthetic", "test-account")),
        patch.object(CodexLLM, "_load_codex_refresh_token", return_value="synthetic"),
    ):
        impl = CodexLLM(provider="openai-codex", api_key="test-key", base_url="", model="synthetic-model")
    provider._provider_impl = impl
    provider.provider = "openai-codex"
    first = "event: response.text.delta\ndata: " + json.dumps({"delta": json.dumps(MISSING)}) + "\n\n"
    first += 'data: {"type":"response.completed","response":{}}\n\n'
    requests = []

    def respond(url, **kwargs):
        requests.append(kwargs)
        return CodexReply(text=first) if len(requests) == 1 else CodexReply(status_code=status, text="synthetic auth")

    cancellation = asyncio.CancelledError()
    # Stub refresh to prevent reading/writing real auth files or contacting OAuth.
    with (
        stub_codex_stream_with(impl, respond),
        patch.object(impl, "_ensure_fresh_token", new_callable=AsyncMock),
        patch.object(
            impl, "_refresh_oauth_tokens", new=AsyncMock(side_effect=cancellation if cancel_refresh else None)
        ),
    ):
        with pytest.raises(asyncio.CancelledError if cancel_refresh else UpstreamHTTPError) as raised:
            await batch(provider, config)
    if cancel_refresh:
        assert raised.value is cancellation
    else:
        assert raised.value.status_code == status
    assert len(requests) == 2


@pytest.mark.asyncio
async def test_unsupported_attempt_gated_provider_makes_no_correction_request(provider, config):
    stub = install(provider, [MISSING, VALID])
    original = provider._provider_impl

    class UnverifiedProvider:
        # Concurrency support is not evidence that SDK-internal retries are bounded.
        def supports_attempt_scoped_concurrency(self):
            return True

        def supports_prompt_caching(self):
            return False

        async def call(self, **kwargs):
            return await original.call(**kwargs)

    provider._provider_impl = UnverifiedProvider()
    budget = c._SchemaCorrectionBudget()
    result = await batch(provider, config, schema_correction_budget=budget)
    assert result.failed
    assert len(stub.requests) == 1
    assert budget.stats.attempts == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["retry", "reject"])
async def test_corrected_response_language_failure_cannot_make_third_completion(provider, config, mode):
    from hindsight_api.engine.language_integrity import GeneratedLanguageMismatch, LanguageCheckResult, LanguageMismatch

    config.llm_language_integrity = mode
    stub = install(provider, [MISSING, VALID, VALID])
    evaluation = LanguageCheckResult(mismatches=(LanguageMismatch("update:0", "en", "fr"),), checked=1, abstained=0)
    with (
        patch.object(c, "prepare_context_safely", new=AsyncMock(return_value=object())),
        patch.object(c, "build_source_instruction", return_value=""),
        patch.object(c, "evaluate_language_integrity_safely", new=AsyncMock(return_value=evaluation)),
    ):
        if mode == "reject":
            with pytest.raises(GeneratedLanguageMismatch):
                await batch(provider, config)
        else:
            result = await batch(provider, config)
            assert result.failed
    assert len(stub.requests) == 2


@pytest.mark.asyncio
async def test_corrected_topology_and_mixed_invalid_actions_reject_whole_response(provider, config):
    invalid = copy.deepcopy(VALID)
    invalid["creates"] = [{"text": "Unattributed sibling", "source_fact_ids": [UNKNOWN]}]
    stub = install(provider, [MISSING, invalid])
    result = await batch(provider, config, per_fact_observation_ids={F: set()})
    assert result.failed and not result.creates and not result.updates
    assert len(stub.requests) == 2


@pytest.mark.asyncio
async def test_correction_failure_counters_and_request_usage(provider, config):
    from hindsight_api.tracing import get_span_recorder

    stub = install(provider, [MISSING, MISSING, VALID])
    budget = c._SchemaCorrectionBudget()
    with patch.object(get_span_recorder(), "record_llm_call") as record:
        result = await batch(provider, config, schema_correction_budget=budget)
    assert result.failed and len(stub.requests) == 2
    assert budget.result_stats() == {
        "schema_correction_initial_failures": 1,
        "schema_correction_attempts": 1,
        "schema_correction_successes": 0,
        "schema_correction_failures": 1,
        "schema_correction_budget_exhausted": 0,
        "schema_correction_context_exhausted": 0,
    }
    errors = [call.kwargs for call in record.call_args_list if call.kwargs.get("error")]
    assert len(errors) == 2
    assert all(entry["input_tokens"] == 10 and entry["output_tokens"] == 12 for entry in errors)


@pytest.mark.asyncio
async def test_constrained_schema_preserves_observation_cap(provider, config):
    stub = install(provider, [MISSING, VALID])
    result = await batch(provider, config, remaining_observation_slots=0)
    assert not result.failed and len(stub.requests) == 2
    feedback = stub.requests[1]["messages"][-1]["content"]
    assert '"maxItems": 0' in feedback


def test_feedback_is_bounded_and_excludes_input_context_and_messages():
    with pytest.raises(ValidationError) as raised:
        c._ConsolidationBatchResponse.model_validate({"updates": [{"reason": "PRIVATE_MALFORMED_VALUE"}] * 1000})
    feedback = c._schema_correction_feedback(raised.value, c._ConsolidationBatchResponse)
    assert feedback is not None and "PRIVATE_MALFORMED_VALUE" not in feedback
    assert "Field required" not in feedback
    assert feedback.count(": missing") == 12


@pytest.mark.asyncio
@pytest.mark.memory_backend_incompatible
async def test_job_budget_survives_scopes_lanes_fetches_bisection_and_isolates_banks(memory, request_context, provider):
    import re
    import uuid

    from hindsight_api.config import _get_raw_config
    from hindsight_api.engine.response_models import RecallResult

    banks = [f"schema-budget-{uuid.uuid4().hex[:8]}" for _ in range(2)]
    for bank_id in banks:
        await memory.ensure_bank_profile(bank_id=bank_id, request_context=request_context)
        # Seed pending facts without running retain's unrelated extraction/model
        # policy. State assertions below use the engine's bank-scoped read API.
        async with memory._pool.acquire() as conn:
            for index in range(24):
                await conn.execute(
                    "INSERT INTO memory_units (id, bank_id, text, fact_type, tags, observation_scopes, created_at) "
                    "VALUES ($1, $2, $3, 'experience', $4, $5::jsonb, now())",
                    uuid.uuid4(),
                    bank_id,
                    f"Synthetic fact {index}",
                    [f"group:{index % 3}", "shared"],
                    json.dumps("per_tag"),
                )

    class DispatchStub(SDKStub):
        async def create(self, **kwargs):
            user = kwargs["messages"][-1]["content"]
            fact_ids = re.findall(r"\[([0-9a-f-]{36})\]", user)
            correction = "Return a COMPLETE replacement" in user
            # Multi-fact corrections remain malformed to drive actual adaptive
            # bisection; only a corrected leaf is allowed to write.
            value = (
                {
                    "creates": [
                        {"text": f"Synthetic corrected observation for {fact_ids[0]}", "source_fact_ids": fact_ids}
                    ]
                }
                if correction and len(fact_ids) == 1
                else {"creates": ["PRIVATE_MALFORMED_VALUE"]}
            )
            self.responses.append(value)
            return await super().create(**kwargs)

    stub = DispatchStub([])
    provider._provider_impl._client = SimpleNamespace(chat=SimpleNamespace(completions=stub))
    raw = _get_raw_config()
    job_config = type(raw)(
        **{
            **{name: getattr(raw, name) for name in raw.__dataclass_fields__},
            "enable_observations": True,
            "consolidation_batch_size": 9,
            "consolidation_max_memories_per_round": 1000,
            "consolidation_llm_batch_size": 4,
            "consolidation_llm_parallelism": 3,
            "consolidation_lane_llm_parallelism": 2,
            "llm_language_integrity": "off",
            "consolidation_dedup_threshold": 1.0,
        }
    )
    original = memory._consolidation_llm_config
    memory._consolidation_llm_config = SimpleNamespace(with_config=lambda config, **kwargs: provider)
    try:
        with (
            patch.object(memory._config_resolver, "resolve_full_config", return_value=job_config),
            patch.object(c, "_find_related_observations", new=AsyncMock(return_value=RecallResult(results=[]))),
            patch.object(memory, "submit_async_consolidation"),
        ):
            # Concurrent independent jobs cannot inherit each other's ContextVar
            # attempt guard or consume each other's round credits.
            results = await asyncio.gather(
                *(c.run_consolidation_job(memory, bank_id, request_context) for bank_id in banks)
            )
        correction_requests = [
            r for r in stub.requests if "Return a COMPLETE replacement" in r["messages"][-1]["content"]
        ]
        assert len(correction_requests) == 20
        for bank_id, result in zip(banks, results, strict=True):
            assert result["status"] == "completed"
            assert result["schema_correction_attempts"] == 10
            assert result["schema_correction_budget_exhausted"] > 0
            assert result["schema_correction_initial_failures"] > 10
            assert result["schema_correction_successes"] + result["schema_correction_failures"] == 10
            failed = await memory.list_memory_units(
                bank_id, consolidation_state="failed", limit=100, request_context=request_context
            )
            pending = await memory.list_memory_units(
                bank_id, consolidation_state="pending", limit=100, request_context=request_context
            )
            observations = await memory.list_memory_units(
                bank_id, fact_type="observation", limit=100, request_context=request_context
            )
            assert failed["total"] == result["memories_failed"] > 0
            assert pending["total"] == 0
            assert observations["total"] == result["observations_created"]
    finally:
        memory._consolidation_llm_config = original
        for bank_id in banks:
            await memory.delete_bank(bank_id, request_context=request_context)


@pytest.mark.asyncio
async def test_context_guard_counts_provider_injected_schema(provider, config):
    stub = install(provider, [MISSING, VALID])
    budget = c._SchemaCorrectionBudget()
    with patch.object(
        c, "count_tokens", side_effect=lambda text: 100 if text.startswith("\n\nYou must respond") else 1
    ):
        config.consolidation_max_context_tokens = 40
        result = await batch(provider, config, schema_correction_budget=budget)
    assert result.failed and len(stub.requests) == 1
    assert budget.stats.attempts == 0 and budget.stats.context_exhausted == 1


@pytest.mark.asyncio
async def test_empty_response_classification_is_not_schema_correction(provider, config):
    from hindsight_api.engine.providers.openai_compatible_llm import ProviderResponseError

    error = ProviderResponseError("Empty content returned")
    assert c._classify_batch_failure(error) is c._BatchFailureClass.RETRY
    config.consolidation_max_attempts = 1
    config.consolidation_llm_max_retries = 0
    stub = install(provider, [error, VALID])
    budget = c._SchemaCorrectionBudget()
    result = await batch(provider, config, schema_correction_budget=budget)
    assert result.failed and len(stub.requests) == 1
    assert budget.stats.initial_failures == budget.stats.attempts == 0


@pytest.mark.asyncio
@pytest.mark.memory_backend_incompatible
@pytest.mark.parametrize("later_failure", ["budget", "invalid", "stale"])
async def test_job_counts_committed_scope_actions_when_a_later_scope_fails(
    memory, request_context, provider, later_failure
):
    import uuid

    from hindsight_api.config import _get_raw_config
    from hindsight_api.engine.response_models import RecallResult

    bank_id = f"schema-partial-count-{uuid.uuid4().hex[:8]}"
    await memory.ensure_bank_profile(bank_id=bank_id, request_context=request_context)
    original = memory._consolidation_llm_config
    try:
        fact_id = uuid.uuid4()
        async with memory._pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO memory_units (id, bank_id, text, fact_type, tags, observation_scopes, created_at) "
                "VALUES ($1, $2, $3, 'experience', $4, $5::jsonb, now())",
                fact_id,
                bank_id,
                "Synthetic fact",
                ["scope:a", "scope:b"],
                json.dumps("per_tag"),
            )
        valid = {"creates": [{"text": "Synthetic scoped observation", "source_fact_ids": [str(fact_id)]}]}
        install(provider, [MISSING, valid, MISSING])
        raw = _get_raw_config()
        job_config = type(raw)(
            **{
                **{name: getattr(raw, name) for name in raw.__dataclass_fields__},
                "enable_observations": True,
                "consolidation_llm_batch_size": 1,
                # One correction credit: scope a commits, scope b cannot correct.
                "consolidation_max_memories_per_round": 100,
                "llm_language_integrity": "off",
                "consolidation_dedup_threshold": 1.0,
            }
        )
        memory._consolidation_llm_config = SimpleNamespace(with_config=lambda config, **kwargs: provider)
        process_batch = c._process_memory_batch

        async def fail_later_scope(**kwargs):
            if kwargs["obs_tags_override"] == ["scope:b"]:
                if later_failure == "invalid":
                    raise c._InvalidConsolidationReferences("synthetic invalid later scope")
                if later_failure == "stale":
                    raise c._StaleConsolidationReference("synthetic stale later scope")
            return await process_batch(**kwargs)

        with (
            patch.object(memory._config_resolver, "resolve_full_config", return_value=job_config),
            patch.object(c, "_find_related_observations", new=AsyncMock(return_value=RecallResult(results=[]))),
            patch.object(c, "_process_memory_batch", new=fail_later_scope),
            patch.object(memory, "submit_async_consolidation"),
        ):
            result = await c.run_consolidation_job(memory, bank_id, request_context)
        observations = await memory.list_memory_units(
            bank_id, fact_type="observation", limit=100, request_context=request_context
        )
        assert observations["total"] == result["observations_created"] == result["actions_executed"] == 1
        assert observations["items"][0]["tags"] == ["scope:a"]
        assert result["memories_processed"] == result["memories_failed"] == (0 if later_failure == "stale" else 1)
        if later_failure == "budget":
            assert result["schema_correction_attempts"] == 1
            assert result["schema_correction_budget_exhausted"] > 0
    finally:
        memory._consolidation_llm_config = original
        await memory.delete_bank(bank_id, request_context=request_context)


@pytest.mark.asyncio
@pytest.mark.memory_backend_incompatible
async def test_corrected_committed_scope_is_not_replayed_on_later_apply_failure(memory, request_context, provider):
    import uuid

    from hindsight_api.config import _get_raw_config
    from hindsight_api.engine.response_models import RecallResult

    bank_id = f"schema-partial-{uuid.uuid4().hex[:8]}"
    await memory.ensure_bank_profile(bank_id=bank_id, request_context=request_context)
    original = memory._consolidation_llm_config
    try:
        fact_id = uuid.uuid4()
        # Direct fixture seeding avoids retain/extraction; assertions use the API.
        async with memory._pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO memory_units (id, bank_id, text, fact_type, tags, observation_scopes, created_at) "
                "VALUES ($1, $2, $3, 'experience', $4, $5::jsonb, now())",
                fact_id,
                bank_id,
                "Synthetic fact",
                ["scope:a", "scope:b"],
                json.dumps("per_tag"),
            )
        valid = {"creates": [{"text": "Synthetic scoped observation", "source_fact_ids": [str(fact_id)]}]}
        stub = install(provider, [MISSING, valid, MISSING, valid])
        raw = _get_raw_config()
        job_config = type(raw)(
            **{
                **{name: getattr(raw, name) for name in raw.__dataclass_fields__},
                "enable_observations": True,
                "consolidation_llm_batch_size": 1,
                "consolidation_max_memories_per_round": 1000,
                "llm_language_integrity": "off",
                "consolidation_dedup_threshold": 1.0,
            }
        )
        memory._consolidation_llm_config = SimpleNamespace(with_config=lambda config, **kwargs: provider)
        apply_create = c._apply_create_observation
        calls = []
        error = RuntimeError("synthetic later-scope apply failure")

        async def fail_second_scope(**kwargs):
            calls.append(kwargs["tags"])
            if kwargs["tags"] == ["scope:b"]:
                raise error
            return await apply_create(**kwargs)

        with (
            patch.object(memory._config_resolver, "resolve_full_config", return_value=job_config),
            patch.object(c, "_find_related_observations", new=AsyncMock(return_value=RecallResult(results=[]))),
            patch.object(c, "_apply_create_observation", new=fail_second_scope),
            patch.object(memory, "submit_async_consolidation"),
        ):
            with pytest.raises(RuntimeError) as raised:
                await c.run_consolidation_job(memory, bank_id, request_context)
        assert raised.value is error
        assert calls == [["scope:a"], ["scope:b"]]
        assert len(stub.requests) == 4
        observations = await memory.list_memory_units(
            bank_id, fact_type="observation", limit=100, request_context=request_context
        )
        pending = await memory.list_memory_units(
            bank_id, consolidation_state="pending", limit=100, request_context=request_context
        )
        assert observations["total"] == 1 and observations["items"][0]["tags"] == ["scope:a"]
        assert pending["total"] == 1
    finally:
        memory._consolidation_llm_config = original
        await memory.delete_bank(bank_id, request_context=request_context)


@pytest.mark.asyncio
@pytest.mark.parametrize("partial", [False, True])
@pytest.mark.parametrize("correction_restores_text", [False, True])
async def test_incomplete_recall_lineage_fails_closed(provider, config, partial, correction_restores_text):
    from hindsight_api.engine.response_models import MemoryFact

    omitted = "55555555-5555-4555-8555-555555555555"
    observation = SimpleNamespace(
        **{**vars(OBSERVATION), "text": "Timeout is 5 seconds.", "source_fact_ids": [UNKNOWN, omitted]}
    )
    # The omitted source is the only support for the numeric anchor. Recall's
    # provenance token cap must not turn missing evidence into permission to erase.
    sources = (
        {UNKNOWN: MemoryFact(id=UNKNOWN, text="Timeout configuration exists.", fact_type="world")} if partial else {}
    )
    first = {"updates": [{"text": "Timeout configuration exists.", "observation_id": OBS_ID, "source_fact_ids": [F]}]}
    corrected = copy.deepcopy(first)
    if correction_restores_text:
        corrected["updates"][0]["text"] = "Timeout is 5 seconds and configuration exists."
    stub = install(provider, [first, corrected])
    result = await c._consolidate_batch_with_llm(provider, MEMORIES, [observation], sources, config)
    assert not result.failed and not result.updates
    assert len(result.creates) == 1 and result.creates[0]._preserve_separate
    assert result.creates[0].source_fact_ids == [F]
    assert result.creates[0].text == first["updates"][0]["text"]
    assert observation.text == "Timeout is 5 seconds."
    assert len(stub.requests) == 1  # No completion can repair unavailable lineage.


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [RuntimeError("synthetic detail transport failure"), MISSING])
async def test_failed_detail_correction_language_mismatch_has_no_third_request(provider, config, failure):
    from hindsight_api.engine.language_integrity import LanguageCheckResult, LanguageMismatch
    from hindsight_api.engine.response_models import MemoryFact

    config.llm_language_integrity = "retry"
    before = "The timeout is 5 seconds."
    observation = SimpleNamespace(**{**vars(OBSERVATION), "text": before, "source_fact_ids": [UNKNOWN]})
    sources = {UNKNOWN: MemoryFact(id=UNKNOWN, text=before, fact_type="world")}
    lossy = {"updates": [{"text": "The timeout is configured.", "observation_id": OBS_ID, "source_fact_ids": [F]}]}
    stub = install(provider, [lossy, failure, VALID])
    evaluation = LanguageCheckResult(mismatches=(LanguageMismatch("create:0", "en", "fr"),), checked=1, abstained=0)
    budget = c._SchemaCorrectionBudget()
    with (
        patch.object(c, "prepare_context_safely", new=AsyncMock(return_value=object())),
        patch.object(c, "build_source_instruction", return_value=""),
        patch.object(c, "evaluate_language_integrity_safely", new=AsyncMock(return_value=evaluation)),
    ):
        result = await c._consolidate_batch_with_llm(
            provider, MEMORIES, [observation], sources, config, schema_correction_budget=budget
        )
    assert len(stub.requests) == 2
    assert result.failed
    assert budget.detail_stats.attempts == budget.detail_stats.correction_failed == 1


@pytest.mark.asyncio
@pytest.mark.memory_backend_incompatible
@pytest.mark.parametrize("match", ["shown", "update"])
@pytest.mark.parametrize("serialized", [False, True])
async def test_fallback_exact_duplicate_persists_all_sources(memory, request_context, provider, match, serialized):
    import uuid
    from dataclasses import replace

    from hindsight_api.config import _get_raw_config
    from hindsight_api.engine.response_models import MemoryFact, RecallResult

    bank_id = f"detail-exact-{uuid.uuid4().hex[:8]}"
    await memory.ensure_bank_profile(bank_id=bank_id, request_context=request_context)
    old_id, target_id, twin_id = [uuid.uuid4() for _ in range(3)]
    new_ids = [uuid.uuid4(), uuid.uuid4()]
    before = "Timeout is 5 seconds and must remain configured."
    proposed = "Timeout remains configured."
    twin_before = proposed if match == "shown" else "Timeout remains configured elsewhere."
    old = MemoryFact(id=str(old_id), text=before, fact_type="world")
    observations = [
        MemoryFact(id=str(target_id), text=before, fact_type="observation", source_fact_ids=[str(old_id)]),
        MemoryFact(id=str(twin_id), text=twin_before, fact_type="observation", source_fact_ids=[str(old_id)]),
    ]
    memories = [{"id": fid, "text": proposed, "tags": []} for fid in new_ids]
    reply = {
        "updates": [
            {"text": proposed, "observation_id": str(target_id), "source_fact_ids": [str(fid) for fid in new_ids]}
        ]
    }
    if match == "update":
        reply["updates"].append(
            {"text": proposed, "observation_id": str(twin_id), "source_fact_ids": [str(new_ids[0])]}
        )
    install(provider, [reply, reply])
    try:
        async with memory._pool.acquire() as conn:
            for fid, text in [(old_id, before), *[(fid, proposed) for fid in new_ids]]:
                await conn.execute(
                    "INSERT INTO memory_units (id, bank_id, text, fact_type, tags, created_at) VALUES ($1, $2, $3, 'world', '{}', now())",
                    fid,
                    bank_id,
                    text,
                )
            for obs in observations:
                await conn.execute(
                    "INSERT INTO memory_units (id, bank_id, text, fact_type, tags, source_memory_ids, created_at) VALUES ($1, $2, $3, 'observation', '{}', $4, now())",
                    uuid.UUID(obs.id),
                    bank_id,
                    obs.text,
                    [old_id],
                )
        config = replace(_get_raw_config(), llm_language_integrity="off", consolidation_dedup_threshold=1.0)
        predecessor, successor = asyncio.Event(), asyncio.Event()
        predecessor.set()
        with (
            patch.object(
                c,
                "_find_related_observations",
                new=AsyncMock(return_value=RecallResult(results=observations, source_facts={str(old_id): old})),
            ),
            patch.object(c, "_embed_observation_text", new=AsyncMock(return_value=None)),
        ):
            await c._process_memory_batch(
                pool=memory._backend,
                memory_engine=memory,
                llm_config=provider,
                bank_id=bank_id,
                memories=memories,
                request_context=request_context,
                config=config,
                mark_consolidated_ids=new_ids,
                apply_turn=(predecessor, successor) if serialized else None,
            )
        # Read the committed database rows, not mocked CREATE calls or stamps.
        async with memory._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT id, text, source_memory_ids FROM memory_units WHERE bank_id = $1 AND fact_type = 'observation'",
                bank_id,
            )
            unchanged = next(row for row in rows if row["id"] == target_id)
            assert unchanged["text"] == before and unchanged["source_memory_ids"] == [old_id]
            fallback = [row for row in rows if row["id"] not in {target_id, twin_id}]
            assert len(fallback) == 1 and fallback[0]["text"] == proposed
            assert set(fallback[0]["source_memory_ids"]) == set(new_ids)
            stamped = await conn.fetch(
                "SELECT id FROM memory_units WHERE bank_id = $1 AND id = ANY($2::uuid[]) AND consolidated_at IS NOT NULL",
                bank_id,
                new_ids,
            )
            assert {row["id"] for row in stamped} == set(new_ids)
    finally:
        await memory.delete_bank(bank_id, request_context=request_context)


@pytest.mark.asyncio
@pytest.mark.memory_backend_incompatible
@pytest.mark.parametrize("mode", ["legitimate", "lossy", "repaired", "chars", "count", "missing", "foreign"])
async def test_guard_hydrates_capped_provenance_without_prompt_or_row_growth(memory, request_context, provider, mode):
    import uuid
    from dataclasses import replace

    from hindsight_api.config import _get_raw_config
    from hindsight_api.engine.response_models import MemoryFact, RecallResult

    bank_id = f"detail-hydration-{uuid.uuid4().hex[:8]}"
    foreign_bank = f"detail-foreign-{uuid.uuid4().hex[:8]}"
    await memory.ensure_bank_profile(bank_id=bank_id, request_context=request_context)
    await memory.ensure_bank_profile(bank_id=foreign_bank, request_context=request_context)
    old_id, target_id = uuid.uuid4(), uuid.uuid4()
    before = "The server timeout is 5 seconds."
    private_text = "GUARD_ONLY_PROVENANCE " + "supporting context " * 300 + before
    assert c.count_tokens(private_text) > 256
    if mode == "chars":
        private_text += "x" * 131073
    prior_ids = [old_id]
    if mode == "count":
        prior_ids += [uuid.uuid4() for _ in range(128)]
    config = replace(_get_raw_config(), llm_language_integrity="off", consolidation_dedup_threshold=1.0)
    rounds = 3 if mode == "legitimate" else 1
    before_hydration_reads = []
    store = c.get_memories()
    original_read = store.get_memory_text_sizes

    async def traced_read(**kwargs):
        if str(old_id) in {str(fid) for fid in kwargs["unit_ids"]}:
            before_hydration_reads.append(kwargs["unit_ids"])
        return await original_read(**kwargs)

    try:
        async with memory._pool.acquire() as conn:
            if mode != "missing":
                await conn.execute(
                    "INSERT INTO memory_units (id, bank_id, text, fact_type, tags, created_at) VALUES ($1, $2, $3, 'world', '{}', now())",
                    old_id,
                    foreign_bank if mode == "foreign" else bank_id,
                    private_text,
                )
            await conn.execute(
                "INSERT INTO memory_units (id, bank_id, text, fact_type, tags, source_memory_ids, created_at) VALUES ($1, $2, $3, 'observation', '{}', $4, now())",
                target_id,
                bank_id,
                before,
                prior_ids,
            )
        stub = install(provider, [])
        for index in range(rounds):
            fact_id = uuid.uuid4()
            good_text = before + " Configuration remains active." * (index + 1)
            proposed = "The server timeout remains active." if mode in {"lossy", "repaired"} else good_text
            reply = {
                "updates": [{"text": proposed, "observation_id": str(target_id), "source_fact_ids": [str(fact_id)]}]
            }
            corrected = copy.deepcopy(reply)
            if mode == "repaired":
                corrected["updates"][0]["text"] = good_text
            stub.responses += [reply, corrected] if mode in {"lossy", "repaired"} else [reply]
            async with memory._pool.acquire() as conn:
                await conn.execute(
                    "INSERT INTO memory_units (id, bank_id, text, fact_type, tags, created_at) VALUES ($1, $2, $3, 'world', '{}', now())",
                    fact_id,
                    bank_id,
                    "The server configuration remains active.",
                )
                # Recall's source budget deliberately omits the old, >256-token
                # fact. Query only fixture lineage, which the read API does not expose.
                row = await conn.fetchrow(
                    "SELECT text, source_memory_ids FROM memory_units WHERE bank_id = $1 AND id = $2",
                    bank_id,
                    target_id,
                )
            obs = MemoryFact(
                id=str(target_id),
                text=row["text"],
                fact_type="observation",
                source_fact_ids=[str(fid) for fid in row["source_memory_ids"]],
            )
            with (
                patch.object(
                    c,
                    "_find_related_observations",
                    new=AsyncMock(return_value=RecallResult(results=[obs], source_facts={})),
                ),
                patch.object(c, "_embed_observation_text", new=AsyncMock(return_value=None)),
                patch.object(store, "get_memory_text_sizes", new=traced_read),
            ):
                await c._process_memory_batch(
                    pool=memory._backend,
                    memory_engine=memory,
                    llm_config=provider,
                    bank_id=bank_id,
                    memories=[{"id": fact_id, "text": "The server configuration remains active.", "tags": []}],
                    request_context=request_context,
                    config=config,
                )
            observations = await memory.list_memory_units(
                bank_id, fact_type="observation", limit=100, request_context=request_context
            )
            if mode in {"legitimate", "repaired"}:
                assert observations["total"] == 1
                assert observations["items"][0]["text"] == good_text
            else:
                assert observations["total"] == 2
                assert next(item for item in observations["items"] if item["id"] == str(target_id))["text"] == before
        assert all("GUARD_ONLY_PROVENANCE" not in json.dumps(request["messages"]) for request in stub.requests)
        if mode in {"lossy", "repaired"}:
            assert len(stub.requests) == 2
            feedback = stub.requests[1]["messages"][-1]["content"]
            assert '"5"' in feedback and "incomplete prior source lineage" not in feedback
        else:
            assert len(stub.requests) == rounds
        assert len(before_hydration_reads) == (0 if mode == "count" else rounds)
    finally:
        await memory.delete_bank(bank_id, request_context=request_context)
        await memory.delete_bank(foreign_bank, request_context=request_context)


@pytest.mark.asyncio
@pytest.mark.parametrize("store_owned", [False, True])
async def test_guard_hydration_is_one_batched_read_and_target_local(provider, config, store_owned):
    import uuid
    from contextlib import asynccontextmanager

    from hindsight_api.engine.response_models import MemoryFact

    char_target = "66666666-6666-4666-8666-666666666666"
    count_target = "77777777-7777-4777-8777-777777777777"
    char_source = "88888888-8888-4888-8888-888888888888"
    good_source = UNKNOWN
    observations = [
        MemoryFact(id=OBS_ID, text="Timeout is 5 seconds.", fact_type="observation", source_fact_ids=[good_source]),
        MemoryFact(
            id=char_target, text="Other timeout is 5 seconds.", fact_type="observation", source_fact_ids=[char_source]
        ),
        MemoryFact(
            id=count_target,
            text="Third timeout is 5 seconds.",
            fact_type="observation",
            source_fact_ids=[str(uuid.UUID(int=index + 1)) for index in range(129)],
        ),
    ]
    loaded = [
        SimpleNamespace(unit_id=good_source, text=observations[0].text, mentioned_at=None),
        SimpleNamespace(unit_id=char_source, text="x" * 131073, mentioned_at=None),
    ]
    store = SimpleNamespace(
        store_owned_for=lambda bank_id: store_owned,
        get_memory_text_sizes=AsyncMock(
            return_value=[
                SimpleNamespace(
                    unit_id=source.unit_id, text_chars=len(source.text), text_bytes=len(source.text.encode("utf-8"))
                )
                for source in loaded
            ]
        ),
        get_memory_evidence=AsyncMock(return_value=loaded[:1]),
    )
    connection = object()

    @asynccontextmanager
    async def acquire(pool):
        yield connection

    reply = {
        "updates": [
            {"text": obs.text + " Configuration active.", "observation_id": obs.id, "source_fact_ids": [F]}
            for obs in observations
        ]
    }
    stub = install(provider, [reply])
    with patch.object(c, "get_memories", return_value=store), patch.object(c, "acquire_with_retry", new=acquire):
        result = await c._consolidate_batch_with_llm(
            provider,
            MEMORIES,
            observations,
            {},
            config,
            detail_guard_pool=object(),
            detail_guard_bank_id="synthetic-bank",
        )
    assert not result.failed
    assert [update.observation_id for update in result.updates] == [OBS_ID]
    assert len(result.creates) == 2 and all(create._preserve_separate for create in result.creates)
    assert len(stub.requests) == 1
    store.get_memory_text_sizes.assert_awaited_once()
    store.get_memory_evidence.assert_awaited_once()
    assert set(store.get_memory_text_sizes.await_args.kwargs["unit_ids"]) == {good_source, char_source}
    assert {size.unit_id for size in store.get_memory_evidence.await_args.kwargs["sizes"]} == {good_source}
    for read in (store.get_memory_text_sizes, store.get_memory_evidence):
        assert read.await_args.kwargs["bank_id"] == "synthetic-bank"
        assert read.await_args.kwargs["conn"] is (None if store_owned else connection)


@pytest.mark.asyncio
async def test_guard_hydration_is_reused_after_language_retry(provider, config):
    from contextlib import asynccontextmanager

    from hindsight_api.engine.language_integrity import LanguageCheckResult, LanguageMismatch
    from hindsight_api.engine.response_models import MemoryFact

    config.llm_language_integrity = "retry"
    observation = MemoryFact(
        id=OBS_ID, text="Timeout is 5 seconds.", fact_type="observation", source_fact_ids=[UNKNOWN]
    )
    store = SimpleNamespace(
        store_owned_for=lambda bank_id: False,
        get_memory_text_sizes=AsyncMock(
            return_value=[
                SimpleNamespace(
                    unit_id=UNKNOWN, text_chars=len(observation.text), text_bytes=len(observation.text.encode("utf-8"))
                )
            ]
        ),
        get_memory_evidence=AsyncMock(
            return_value=[SimpleNamespace(unit_id=UNKNOWN, text=observation.text, mentioned_at=None)]
        ),
    )

    @asynccontextmanager
    async def acquire(pool):
        yield object()

    reply = {
        "updates": [
            {"text": observation.text + " Configuration active.", "observation_id": OBS_ID, "source_fact_ids": [F]}
        ]
    }
    stub = install(provider, [reply, reply])
    evaluations = [
        LanguageCheckResult(mismatches=(LanguageMismatch("update:0", "en", "fr"),), checked=1, abstained=0),
        LanguageCheckResult(mismatches=(), checked=1, abstained=0),
    ]
    with (
        patch.object(c, "get_memories", return_value=store),
        patch.object(c, "acquire_with_retry", new=acquire),
        patch.object(c, "prepare_context_safely", new=AsyncMock(return_value=object())),
        patch.object(c, "build_source_instruction", return_value=""),
        patch.object(c, "evaluate_language_integrity_safely", new=AsyncMock(side_effect=evaluations)),
    ):
        result = await c._consolidate_batch_with_llm(
            provider,
            MEMORIES,
            [observation],
            {},
            config,
            detail_guard_pool=object(),
            detail_guard_bank_id="synthetic-bank",
        )
    assert not result.failed and len(result.updates) == 1
    assert len(stub.requests) == 2
    store.get_memory_text_sizes.assert_awaited_once()
    store.get_memory_evidence.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.memory_backend_incompatible
async def test_exact_serialized_fold_veto_inserts_and_attributes_new_source(memory, request_context, provider):
    import uuid
    from dataclasses import replace

    from hindsight_api.config import _get_raw_config
    from hindsight_api.engine.response_models import RecallResult

    bank_id = f"detail-exact-veto-{uuid.uuid4().hex[:8]}"
    await memory.ensure_bank_profile(bank_id=bank_id, request_context=request_context)
    old_id, new_id, target_id = [uuid.uuid4() for _ in range(3)]
    # The identical fold inputs together exceed the lexical work guard's bound.
    text = "Plain preserved narrative. " * 5100
    config = replace(_get_raw_config(), llm_language_integrity="off", consolidation_dedup_threshold=1.0)
    stub = install(provider, [{"creates": [{"text": text, "source_fact_ids": [str(new_id)]}]}])
    predecessor, successor = asyncio.Event(), asyncio.Event()
    predecessor.set()
    try:
        async with memory._pool.acquire() as conn:
            for fact_id in [old_id, new_id]:
                await conn.execute(
                    "INSERT INTO memory_units (id, bank_id, text, fact_type, tags, created_at) VALUES ($1,$2,$3,'world','{}',now())",
                    fact_id,
                    bank_id,
                    "A new supported fact.",
                )
            await conn.execute(
                "INSERT INTO memory_units (id, bank_id, text, fact_type, tags, source_memory_ids, created_at) VALUES ($1,$2,$3,'observation','{}',$4,now())",
                target_id,
                bank_id,
                text,
                [old_id],
            )
        with (
            patch.object(
                c, "_find_related_observations", new=AsyncMock(return_value=RecallResult(results=[], source_facts={}))
            ),
            patch.object(c, "_embed_observation_text", new=AsyncMock(return_value=None)),
        ):
            await c._process_memory_batch(
                pool=memory._backend,
                memory_engine=memory,
                llm_config=provider,
                bank_id=bank_id,
                memories=[{"id": new_id, "text": "A new supported fact.", "tags": []}],
                request_context=request_context,
                config=config,
                mark_consolidated_ids=[new_id],
                apply_turn=(predecessor, successor),
            )
        assert len(stub.requests) == 1
        # The public read API doesn't expose source_memory_ids or consolidation
        # stamps: read committed rows to prove attribution, not just CREATE calls.
        async with memory._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT id, text, source_memory_ids FROM memory_units WHERE bank_id=$1 AND fact_type='observation'",
                bank_id,
            )
            stamped = await conn.fetchval(
                "SELECT consolidated_at IS NOT NULL FROM memory_units WHERE bank_id=$1 AND id=$2",
                bank_id,
                new_id,
            )
        assert any(new_id in (row["source_memory_ids"] or []) for row in rows), "New source has no durable attribution"
        assert stamped
        old = next(row for row in rows if row["id"] == target_id)
        assert old["text"] == text and old["source_memory_ids"] == [old_id]
        assert len(rows) == 2
    finally:
        await memory.delete_bank(bank_id=bank_id, request_context=request_context)


@pytest.mark.asyncio
@pytest.mark.memory_backend_incompatible
async def test_guard_hydration_rejects_oversize_before_loading_body(memory, request_context):
    import uuid
    from contextlib import asynccontextmanager

    from hindsight_api.engine.response_models import MemoryFact

    bank_id = f"detail-size-read-{uuid.uuid4().hex[:8]}"
    await memory.ensure_bank_profile(bank_id=bank_id, request_context=request_context)
    source_id = uuid.uuid4()
    observation = MemoryFact(
        id=OBS_ID, text="Timeout is 5 seconds.", fact_type="observation", source_fact_ids=[str(source_id)]
    )
    queries = []

    class NarrowReadSpy:
        def __init__(self, conn):
            self.conn = conn

        async def fetch(self, query, *args):
            queries.append(query)
            # Size metadata may inspect text on the server, but may not return
            # it (or unrelated context/metadata) to the Python caller.
            projection = query.lower().split("from", 1)[0]
            assert "char_length(text)" in projection and "octet_length(text)" in projection
            assert "context" not in projection and "metadata" not in projection
            assert "id, text," not in projection
            return await self.conn.fetch(query, *args)

    @asynccontextmanager
    async def acquire(pool):
        async with memory._pool.acquire() as conn:
            yield NarrowReadSpy(conn)

    try:
        async with memory._pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO memory_units (id, bank_id, text, fact_type, tags, created_at) VALUES ($1,$2,$3,'world','{}',now())",
                source_id,
                bank_id,
                "x" * 1_000_000,
            )
        with patch.object(c, "acquire_with_retry", new=acquire):
            evidence = await c._hydrate_detail_guard_evidence([observation], {}, memory._backend, bank_id)
        assert evidence.unavailable == {OBS_ID} and not evidence.sources
        assert len(queries) == 1  # No body read for this target, not even a truncated one.
    finally:
        await memory.delete_bank(bank_id=bank_id, request_context=request_context)


@pytest.mark.asyncio
@pytest.mark.parametrize("bound", ["chars", "sources", "bytes"])
async def test_guard_hydration_enforces_batch_caps_before_body_read(provider, config, bound):
    import uuid
    from contextlib import asynccontextmanager

    from hindsight_api.engine.response_models import MemoryFact

    observations, loaded = [], []
    per_target = 128 if bound == "sources" else 1
    source_text = "x" * 131072 if bound == "chars" else ("界" * 131072 if bound == "bytes" else "Timeout is 5 seconds.")
    accepted = 2 if bound == "bytes" else 4
    for index in range(6):
        source_ids = [str(uuid.uuid4()) for _ in range(per_target)]
        observations.append(
            MemoryFact(
                id=str(uuid.uuid4()), text="Timeout is 5 seconds.", fact_type="observation", source_fact_ids=source_ids
            )
        )
        loaded.extend(SimpleNamespace(unit_id=fid, text=source_text, mentioned_at=None) for fid in source_ids)
    sizes = [
        SimpleNamespace(
            unit_id=source.unit_id, text_chars=len(source.text), text_bytes=len(source.text.encode("utf-8"))
        )
        for source in loaded
    ]

    async def bounded_read(**kwargs):
        ids = {size.unit_id for size in kwargs["sizes"]}
        selected = [source for source in loaded if source.unit_id in ids]
        assert len(selected) <= 512
        assert sum(len(source.text) for source in selected) <= 524288
        assert sum(len(source.text.encode("utf-8")) for source in selected) <= 1048576
        return selected

    store = SimpleNamespace(
        store_owned_for=lambda bank_id: False,
        get_memories=AsyncMock(return_value=loaded),
        get_memory_text_sizes=AsyncMock(return_value=sizes),
        get_memory_evidence=AsyncMock(side_effect=bounded_read),
    )

    @asynccontextmanager
    async def acquire(pool):
        yield object()

    reply = {
        "updates": [
            {"text": obs.text + " Configuration active.", "observation_id": obs.id, "source_fact_ids": [F]}
            for obs in observations
        ]
    }
    stub = install(provider, [reply])
    with patch.object(c, "get_memories", return_value=store), patch.object(c, "acquire_with_retry", new=acquire):
        result = await c._consolidate_batch_with_llm(
            provider,
            MEMORIES,
            observations,
            {},
            config,
            detail_guard_pool=object(),
            detail_guard_bank_id="synthetic-bank",
        )
    assert [update.observation_id for update in result.updates] == [obs.id for obs in observations[:accepted]]
    assert len(result.creates) == 6 - accepted and all(create._preserve_separate for create in result.creates)
    assert len(stub.requests) == 1  # Excessive evidence is not repairable by a completion.
    store.get_memories.assert_not_awaited()
    store.get_memory_text_sizes.assert_awaited_once()
    store.get_memory_evidence.assert_awaited_once()
    assert {size.unit_id for size in store.get_memory_evidence.await_args.kwargs["sizes"]} == {
        fid for obs in observations[:accepted] for fid in obs.source_fact_ids
    }


@pytest.mark.asyncio
@pytest.mark.memory_backend_incompatible
async def test_guard_batch_cap_keeps_admitted_updates_in_place(memory, request_context, provider):
    import uuid
    from dataclasses import replace

    from hindsight_api.config import _get_raw_config
    from hindsight_api.engine.response_models import MemoryFact, RecallResult

    bank_id = f"detail-batch-cap-{uuid.uuid4().hex[:8]}"
    await memory.ensure_bank_profile(bank_id=bank_id, request_context=request_context)
    old_ids, new_ids, target_ids = [[uuid.uuid4() for _ in range(3)] for _ in range(3)]
    before = "Timeout is 5 seconds."
    after = before + " Configuration active."
    observations = [
        MemoryFact(id=str(oid), text=before, fact_type="observation", source_fact_ids=[str(fid)])
        for oid, fid in zip(target_ids, old_ids)
    ]
    reply = {
        "updates": [
            {"text": after, "observation_id": str(oid), "source_fact_ids": [str(fid)]}
            for oid, fid in zip(target_ids, new_ids)
        ]
    }
    stub = install(provider, [reply])
    config = replace(_get_raw_config(), llm_language_integrity="off", consolidation_dedup_threshold=1.0)
    try:
        async with memory._pool.acquire() as conn:
            for fid in old_ids + new_ids:
                await conn.execute(
                    "INSERT INTO memory_units (id, bank_id, text, fact_type, tags, created_at) VALUES ($1,$2,$3,'world','{}',now())",
                    fid,
                    bank_id,
                    before if fid in old_ids else "Configuration active.",
                )
            for oid, fid in zip(target_ids, old_ids):
                await conn.execute(
                    "INSERT INTO memory_units (id, bank_id, text, fact_type, tags, source_memory_ids, created_at) VALUES ($1,$2,$3,'observation','{}',$4,now())",
                    oid,
                    bank_id,
                    before,
                    [fid],
                )
        with (
            patch.object(c, "_DETAIL_GUARD_MAX_BATCH_SOURCE_CHARS", len(before) * 2),
            patch.object(
                c,
                "_find_related_observations",
                new=AsyncMock(return_value=RecallResult(results=observations, source_facts={})),
            ),
            patch.object(c, "_embed_observation_text", new=AsyncMock(return_value=None)),
        ):
            await c._process_memory_batch(
                pool=memory._backend,
                memory_engine=memory,
                llm_config=provider,
                bank_id=bank_id,
                memories=[{"id": fid, "text": "Configuration active.", "tags": []} for fid in new_ids],
                request_context=request_context,
                config=config,
                mark_consolidated_ids=new_ids,
            )
        assert len(stub.requests) == 1
        units = await memory.list_memory_units(
            bank_id, fact_type="observation", limit=100, request_context=request_context
        )
        assert units["total"] == 4
        for oid in target_ids[:2]:
            assert next(unit for unit in units["items"] if unit["id"] == str(oid))["text"] == after
        assert next(unit for unit in units["items"] if unit["id"] == str(target_ids[2]))["text"] == before
        # Lineage and stamping are not exposed by the public read API.
        async with memory._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT id, source_memory_ids FROM memory_units WHERE bank_id=$1 AND fact_type='observation'", bank_id
            )
            stamps = await conn.fetch(
                "SELECT id FROM memory_units WHERE bank_id=$1 AND id=ANY($2::uuid[]) AND consolidated_at IS NOT NULL",
                bank_id,
                new_ids,
            )
        for oid, fid in zip(target_ids[:2], new_ids[:2]):
            assert fid in next(row for row in rows if row["id"] == oid)["source_memory_ids"]
        fallback = next(row for row in rows if row["id"] not in target_ids)
        assert fallback["source_memory_ids"] == [new_ids[2]]
        assert {row["id"] for row in stamps} == set(new_ids)
    finally:
        await memory.delete_bank(bank_id=bank_id, request_context=request_context)


@pytest.mark.asyncio
@pytest.mark.memory_backend_incompatible
async def test_guard_hydration_excludes_body_that_grows_after_size_read(memory, request_context):
    import uuid
    from contextlib import asynccontextmanager

    from hindsight_api.engine.response_models import MemoryFact

    bank_id = f"detail-size-race-{uuid.uuid4().hex[:8]}"
    await memory.ensure_bank_profile(bank_id=bank_id, request_context=request_context)
    source_id = uuid.uuid4()
    obs = MemoryFact(id=OBS_ID, text="Timeout is 5 seconds.", fact_type="observation", source_fact_ids=[str(source_id)])
    queries = []

    class GrowingSource:
        def __init__(self, conn):
            self.conn = conn

        async def fetch(self, query, *args):
            queries.append(query)
            rows = await self.conn.fetch(query, *args)
            if len(queries) == 1:
                await self.conn.execute(
                    "UPDATE memory_units SET text=$1 WHERE bank_id=$2 AND id=$3", "x" * 1_000_000, bank_id, source_id
                )
            else:
                assert "SELECT id, text, mentioned_at" in query
                assert "char_length(text) = $" in query and "octet_length(text) = $" in query
                assert not rows  # An oversized changed body never crossed the SQL boundary.
            return rows

    @asynccontextmanager
    async def acquire(pool):
        async with memory._pool.acquire() as conn:
            yield GrowingSource(conn)

    try:
        async with memory._pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO memory_units (id, bank_id, text, fact_type, tags, created_at) VALUES ($1,$2,$3,'world','{}',now())",
                source_id,
                bank_id,
                obs.text,
            )
        with patch.object(c, "acquire_with_retry", new=acquire):
            evidence = await c._hydrate_detail_guard_evidence([obs], {}, memory._backend, bank_id)
        assert evidence.unavailable == {OBS_ID} and not evidence.sources
        assert len(queries) == 2
    finally:
        await memory.delete_bank(bank_id=bank_id, request_context=request_context)
