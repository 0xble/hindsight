"""Private HTTP reads use the public read handlers without persistent diagnostics.

All payloads and sinks in this module are isolated fixtures. No live providers or
production database are used.
"""

import asyncio
import logging
from unittest.mock import AsyncMock

import httpx
import pytest

from hindsight_api.api import create_app
from hindsight_api.config_resolver import ConfigResolver
from hindsight_api.engine.audit import AuditLogger
from hindsight_api.engine.memory_engine import MemoryEngine
from hindsight_api.engine.response_models import MemoryFact, RecallResult, ReflectResult
from hindsight_api.extensions.builtin.tenant import ApiKeyTenantExtension

QUERY = "private-query-canary-7f349"
ANSWER = "private-answer-canary-8ca72"


@pytest.fixture
def http_reads(monkeypatch):
    """Real HTTP handlers with only the engine's expensive retrieval seam stubbed."""
    engine = MemoryEngine.__new__(MemoryEngine)
    engine._tenant_extension = ApiKeyTenantExtension({"api_key": "fixture-key"})
    engine._operation_validator = None
    engine._config_resolver = ConfigResolver(None, engine._tenant_extension)
    engine._config_resolver._load_bank_config = AsyncMock(return_value={"audit_log_enabled": True})
    audit = AuditLogger(lambda: None, lambda: "public", enabled=True, allowed_actions=[])
    audit._safe_log = AsyncMock()
    engine._audit_logger = audit
    engine._backend = None
    engine._initialized = True
    # Nonempty results always pass through native attachment enrichment. Stub
    # only its external lookup, not the public handler or its expected status.
    engine.attachments_for_memories = AsyncMock(return_value={})
    engine.resolve_bank_alias = AsyncMock(side_effect=lambda bank_id, **kwargs: bank_id)

    async def read(**kwargs):
        await engine._authenticate_tenant(kwargs["request_context"])
        logging.getLogger("fixture.provider").warning("provider input %s output %s", kwargs["query"], ANSWER)
        if kwargs.get("max_tokens") == 13:
            raise RuntimeError(QUERY)
        if "fact_type" in kwargs:
            return RecallResult(
                results=[MemoryFact(id="fixture-fact", text=ANSWER, fact_type="world", attachment_ids=[])],
                trace={"query": kwargs["query"]},
            )
        return ReflectResult(text=ANSWER, based_on={})

    engine.recall_async = AsyncMock(side_effect=read)
    engine.reflect_async = AsyncMock(side_effect=read)
    app = create_app(engine, initialize_memory=False)
    return app, engine, audit


@pytest.mark.asyncio
async def test_private_capability_is_bodyless_get(http_reads):
    app, _, _ = http_reads
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://fixture") as client:
        response = await client.get("/version")
    assert response.status_code == 200
    assert response.json()["features"]["private_reads"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["recall", "reflect"])
async def test_private_alias_reuses_real_handler_and_auth_without_audit(http_reads, caplog, operation):
    app, engine, audit = http_reads
    caplog.set_level(logging.DEBUG)
    body = {"query": QUERY, "tag_groups": [{"tags": ["scoped"], "match": "all"}]}
    if operation == "recall":
        body["trace"] = True
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://fixture") as client:
        denied = await client.post(f"/v1/default/banks/fixture/private/{operation}", json=body)
        assert denied.status_code == 401
        response = await client.post(
            f"/v1/default/banks/fixture/private/{operation}",
            json=body,
            headers={"Authorization": "Bearer fixture-key"},
        )
    await asyncio.sleep(0)
    assert response.status_code == 200, response.text
    if operation == "recall":
        assert response.json()["results"][0]["text"] == ANSWER
    else:
        assert response.json()["text"] == ANSWER
    assert response.headers["X-Hindsight-Private-Read"] == "1"
    audit._safe_log.assert_not_called()
    assert QUERY not in caplog.text
    assert ANSWER not in caplog.text
    called = getattr(engine, f"{operation}_async").call_args.kwargs
    assert called["tags"] is None
    assert called["tag_groups"] is not None
    assert called["request_context"].internal is False


@pytest.mark.asyncio
@pytest.mark.parametrize("operation,public_path", [("recall", "memories/recall"), ("reflect", "reflect")])
async def test_public_read_still_audits_and_logs(http_reads, caplog, operation, public_path):
    app, _, audit = http_reads
    caplog.set_level(logging.DEBUG)
    body = {"query": QUERY}
    if operation == "recall":
        body["trace"] = True
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://fixture") as client:
        response = await client.post(
            f"/v1/default/banks/fixture/{public_path}",
            json=body,
            headers={"Authorization": "Bearer fixture-key"},
        )
    await asyncio.sleep(0)
    assert response.status_code == 200, response.text
    assert "X-Hindsight-Private-Read" not in response.headers
    audit._safe_log.assert_called_once()
    assert QUERY in caplog.text
    assert ANSWER in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "document_id",
    [
        "evil/private/recall",
        "evil/private/reflect",
        "v1/default/banks/other/private/recall",
        "v1/default/banks/other/private/reflect",
    ],
)
@pytest.mark.parametrize("method", ["DELETE", "PATCH"])
@pytest.mark.parametrize("fails", [False, True])
async def test_public_document_suffix_keeps_audit_and_error_diagnostics(
    http_reads, caplog, document_id: str, method: str, fails: bool
) -> None:
    app, engine, audit = http_reads
    caplog.set_level(logging.DEBUG)
    # Keep the real route, request parser and audit wrapper. Only the isolated
    # fixture's persistence seam is replaced, as in the neighboring HTTP reads.
    if method == "DELETE":
        engine.delete_document = AsyncMock(return_value={"document_deleted": 1, "memory_units_deleted": 0})
        action = "delete_document"
    else:
        engine.update_document = AsyncMock(return_value=True)
        action = "update_document"
    mutation = getattr(engine, action)
    if fails:
        mutation.side_effect = RuntimeError("public-document-error")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://fixture") as client:
        response = await client.request(
            method,
            f"/v1/default/banks/fixture/documents/{document_id}",
            json={"tags": ["public-document"]} if method == "PATCH" else None,
            headers={"Authorization": "Bearer fixture-key"},
        )
    await asyncio.sleep(0)
    assert response.status_code == (500 if fails else 200), response.text
    assert "X-Hindsight-Private-Read" not in response.headers
    assert response.headers.get("Cache-Control") != "no-store"
    mutation.assert_awaited_once()
    assert mutation.call_args.args == (document_id, "fixture")
    audit._safe_log.assert_called_once()
    assert audit._safe_log.call_args.args[0].action == action
    if fails:
        assert "public-document-error" in caplog.text


@pytest.mark.asyncio
async def test_private_validation_and_error_do_not_persist(http_reads, caplog):
    app, _, audit = http_reads
    caplog.set_level(logging.DEBUG)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://fixture") as client:
        for body, status in [({"query": {QUERY: ANSWER}}, 422), ({"query": QUERY, "max_tokens": 13}, 500)]:
            response = await client.post(
                "/v1/default/banks/fixture/private/reflect",
                json=body,
                headers={"Authorization": "Bearer fixture-key"},
            )
            assert response.status_code == status, response.text
            assert "X-Hindsight-Private-Read" not in response.headers
    assert QUERY not in caplog.text
    assert ANSWER not in caplog.text


@pytest.mark.asyncio
async def test_private_audit_decision_precedes_resolver_and_capture(monkeypatch):
    from hindsight_api.engine.audit import AuditEntry
    from hindsight_api.private_reads import private_read_scope

    resolver = AsyncMock(side_effect=RuntimeError(QUERY))
    audit = AuditLogger(
        lambda: None, lambda: "public", enabled=True, allowed_actions=[], bank_enabled_resolver=resolver
    )
    schedule = AsyncMock()
    monkeypatch.setattr(audit._writes, "schedule", schedule)
    with private_read_scope():
        assert not audit.action_allowed("reflect")
        assert not await audit.should_log("reflect", "fixture")
        audit.log_fire_and_forget(AuditEntry(action="reflect", transport="http", request={"query": QUERY}))
    resolver.assert_not_called()
    schedule.assert_not_called()
    # Resolver failure must STILL fall back to the global switch publicly.
    assert await audit.should_log("reflect", "fixture")
    resolver.assert_awaited_once()


@pytest.mark.asyncio
async def test_private_recorders_reject_payload_before_scheduling(monkeypatch):
    from unittest.mock import MagicMock

    from hindsight_api import tracing
    from hindsight_api.engine.llm_trace import LLMTraceRecorder
    from hindsight_api.private_reads import private_read_scope

    tracer = MagicMock()
    span_recorder = tracing.LLMSpanRecorder(tracer)
    db_recorder = LLMTraceRecorder(lambda: None, lambda: "public", enabled=True, allowed_scopes=[])
    db_recorder._record_fire_and_forget = MagicMock()
    consumer = MagicMock()
    composite = tracing.CompositeSpanRecorder()
    composite.register(consumer)
    composite.register(db_recorder)
    composite.register(span_recorder)
    event = dict(
        provider="mock",
        model="fixture",
        scope="reflect",
        messages=[{"role": "user", "content": QUERY}],
        response_content=ANSWER,
        error=RuntimeError(QUERY),
        input_tokens=3,
        output_tokens=4,
        duration=0.1,
    )
    with private_read_scope():
        assert not db_recorder.is_enabled("reflect")
        composite.record_llm_call(**event)
        db_recorder.record_llm_call(**event)
        span_recorder.record_llm_call(**event)
        assert not tracing.is_tracing_enabled()
        with tracing.get_tracer().start_as_current_span("hindsight.reflect") as span:
            span.set_attribute("hindsight.query", QUERY)
            span.record_exception(RuntimeError(ANSWER))
    consumer.record_llm_call.assert_not_called()
    db_recorder._record_fire_and_forget.assert_not_called()
    tracer.start_as_current_span.assert_not_called()
    composite.record_llm_call(**event)
    consumer.record_llm_call.assert_called_once()
    db_recorder._record_fire_and_forget.assert_called_once()


@pytest.mark.asyncio
async def test_private_scope_nesting_concurrency_background_and_cancellation(caplog):
    from hindsight_api.private_reads import is_private_read, private_read_scope

    caplog.set_level(logging.DEBUG)
    started = asyncio.Event()
    release = asyncio.Event()
    tasks = []

    async def descendant():
        await release.wait()
        assert is_private_read()
        logging.getLogger("fixture.background").error("%s %s", QUERY, ANSWER)

    async def private():
        with private_read_scope():
            with private_read_scope():
                assert is_private_read()
            assert is_private_read()
            tasks.append(asyncio.create_task(descendant()))
            started.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(private())
    await started.wait()
    assert not is_private_read()
    logging.getLogger("fixture.public").warning("public-content-kept")
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    release.set()
    await asyncio.gather(*tasks)
    assert not is_private_read()
    assert "public-content-kept" in caplog.text
    assert QUERY not in caplog.text
    assert ANSWER not in caplog.text
    with pytest.raises(RuntimeError), private_read_scope():
        raise RuntimeError(QUERY)
    assert not is_private_read()


@pytest.mark.asyncio
async def test_real_reranker_executor_keeps_private_scope(caplog):
    from hindsight_api.engine.cross_encoder import LocalSTCrossEncoder
    from hindsight_api.private_reads import is_private_read, private_read_scope

    caplog.set_level(logging.DEBUG)
    seen = []
    encoder = LocalSTCrossEncoder.__new__(LocalSTCrossEncoder)
    encoder._model = object()

    def predict(pairs):
        seen.append(is_private_read())
        logging.getLogger("fixture.executor").warning("%s %s", *pairs[0])
        return [0.75]

    encoder._predict_sync = predict
    with private_read_scope():
        assert await encoder._predict([(QUERY, ANSWER)]) == [0.75]
    assert seen == [True]
    assert QUERY not in caplog.text
    assert ANSWER not in caplog.text
    await encoder._predict([("public-executor-kept", "public-answer-kept")])
    assert seen == [True, False]
    assert "public-executor-kept" in caplog.text
