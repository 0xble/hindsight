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
        api_key="synthetic-not-a-credential",
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
        llm = LLMProvider(provider="openai", api_key="synthetic", base_url="https://example.invalid", model="stub")
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

    async def fetch(conn, bank_id, fact_types, limit, scopes, deferred):
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
        impl = CodexLLM(provider="openai-codex", api_key="synthetic", base_url="", model="synthetic-model")
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
    assert len(stub.requests) <= 2


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
