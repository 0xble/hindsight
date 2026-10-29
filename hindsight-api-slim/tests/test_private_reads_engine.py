"""Real read pipelines with isolated external I/O, and enabled diagnostic owners.

Unlike test_private_reads' route controls, neither engine read, the retrieval
assembler/fusion/reranker, reflect agent/tools nor diagnostic producers are
replaced here. The fake store arm is the external DB seam and the native mock
LLM is the external provider seam. No server, provider, or real pool is used.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
import pytest_asyncio
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from hindsight_api import tracing
from hindsight_api.api.http import FeaturesInfo, create_app
from hindsight_api.config import _get_raw_config, clear_config_cache
from hindsight_api.config_resolver import ConfigResolver
from hindsight_api.engine import audit, llm_trace, memories
from hindsight_api.engine.audit import AuditLogger
from hindsight_api.engine.cross_encoder import RRFPassthroughCrossEncoder
from hindsight_api.engine.embeddings import OpenAIEmbeddings
from hindsight_api.engine.llm_trace import LLMTraceRecorder
from hindsight_api.engine.llm_wrapper import LLMConfig
from hindsight_api.engine.memories import RecallArms
from hindsight_api.engine.memories.postgres import PostgresMemories
from hindsight_api.engine.memory_engine import DirectivePage, MemoryEngine
from hindsight_api.engine.providers.mock_llm import _user_text_of
from hindsight_api.engine.query_analyzer import DateparserQueryAnalyzer
from hindsight_api.engine.response_models import LLMToolCall, LLMToolCallResult, MemoryFact
from hindsight_api.engine.search.reranking import CrossEncoderReranker
from hindsight_api.engine.search.types import RetrievalResult
from hindsight_api.extensions.builtin.tenant import ApiKeyTenantExtension
from hindsight_api.private_reads import is_private_read

FACT_ID = "00000000-0000-0000-0000-000000000016"
FACT_TEXT = "The Caldera fixture's recovery phrase is paper heron."
PRIVATE_QUERY = "private-query-47b8: what is Caldera's recovery phrase?"
PUBLIC_QUERY = "public-query-819a: what is Caldera's recovery phrase?"
HEADERS = {"Authorization": "Bearer fixture-only-key"}


@dataclass
class ExecutedWrite:
    sql: str
    arguments: tuple[Any, ...]


@dataclass
class IsolatedSink:
    path: Path
    writes: list[ExecutedWrite] = field(default_factory=list)

    async def execute(self, sql: str, *arguments: Any) -> None:
        self.writes.append(ExecutedWrite(sql, arguments))
        with self.path.open("a") as output:
            output.write(json.dumps({"sql": sql, "arguments": arguments}, default=str) + "\n")

    def bytes(self) -> bytes:
        return self.path.read_bytes() if self.path.exists() else b""


@dataclass
class ReadHarness:
    engine: MemoryEngine
    store: PostgresMemories
    sink: IsolatedSink
    exporter: InMemorySpanExporter
    config: Any
    retrievals: list[dict[str, Any]]

    async def drain(self) -> None:
        await self.engine._audit_logger.drain()
        await self.engine._llm_recorder._writes.drain_all(5)

    def span_text(self) -> str:
        return "\n".join(span.to_json() for span in self.exporter.get_finished_spans())


def _provider_answer(messages: list[dict[str, Any]], _scope: str) -> LLMToolCallResult:
    """A scripted provider must derive its answer from the real tool return."""
    tool_returns = [
        json.loads(_user_text_of(message["content"])) for message in messages if message.get("role") == "tool"
    ]
    facts = [fact for result in tool_returns for fact in result.get("memories", [])]
    if not facts:
        return LLMToolCallResult(
            tool_calls=[
                LLMToolCall(
                    id="retrieve",
                    name="recall",
                    arguments={"query": _user_text_of(messages[-1]["content"]), "reason": "look up the fixture fact"},
                )
            ],
            finish_reason="tool_calls",
        )
    return LLMToolCallResult(
        tool_calls=[
            LLMToolCall(
                id="answer", name="done", arguments={"answer": facts[0]["text"], "memory_ids": [facts[0]["id"]]}
            )
        ],
        finish_reason="tool_calls",
    )


@pytest_asyncio.fixture
async def read_harness(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setenv("HINDSIGHT_API_AUDIT_LOG_ENABLED", "true")
    monkeypatch.setenv("HINDSIGHT_API_LLM_TRACE_ENABLED", "true")
    monkeypatch.setenv("HINDSIGHT_API_OTEL_TRACES_ENABLED", "true")
    monkeypatch.setenv("HINDSIGHT_API_LLM_PROVIDER", "mock")
    monkeypatch.setenv("HINDSIGHT_API_EMBEDDINGS_PROVIDER", "openai")
    monkeypatch.setenv("HINDSIGHT_API_RERANKER_PROVIDER", "rrf")
    monkeypatch.setenv("HINDSIGHT_API_REFLECT_MAX_ITERATIONS", "10")
    # Exercise the instrumentor's real operator override in the HTTP controls.
    monkeypatch.setenv("OTEL_PYTHON_FASTAPI_EXCLUDED_URLS", "health")
    clear_config_cache()
    config = _get_raw_config()
    sink = IsolatedSink(tmp_path / "diagnostics.jsonl")
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(trace, "_TRACER_PROVIDER", provider)
    monkeypatch.setattr(tracing, "_tracer", provider.get_tracer("private-read-fixture"))
    monkeypatch.setattr(tracing, "_tracing_enabled", True)

    @asynccontextmanager
    async def connection(_pool: object, **_kwargs: Any) -> AsyncIterator[IsolatedSink]:
        yield sink

    monkeypatch.setattr(audit, "acquire_with_retry", connection)
    monkeypatch.setattr(llm_trace, "acquire_with_retry", connection)
    engine = MemoryEngine.__new__(MemoryEngine)
    engine._tenant_extension = ApiKeyTenantExtension({"api_key": "fixture-only-key"})
    # State the released constructor initializes and reflect now reads.
    engine._reflect_decision_model_client = None
    engine._reflect_decision_model_unsupported_logged = False
    engine._operation_validator = None
    engine._initialized = True
    engine._backend = object()
    engine._read_backend = engine._backend
    engine._search_semaphore = asyncio.Semaphore(2)
    engine._config_resolver = ConfigResolver(engine._backend, engine._tenant_extension)
    # Only the actual bank-config SELECT is stubbed, resolution/auth stay native.
    engine._config_resolver._load_bank_config = AsyncMock(
        return_value={"enable_temporal_retrieval": False, "recall_include_chunks": False, "reflect_llm_max_retries": 1}
    )
    engine.embeddings = OpenAIEmbeddings.__new__(OpenAIEmbeddings)
    engine.query_analyzer = DateparserQueryAnalyzer(languages=["en"])
    engine._cross_encoder_reranker = CrossEncoderReranker(RRFPassthroughCrossEncoder())
    engine._cross_encoder_reranker._initialized = True
    engine._reflect_llm_config = LLMConfig(
        provider="mock", api_key="", model="fixture-only", base_url="", max_retries=1
    )
    engine._reflect_llm_config.set_response_callback(_provider_answer)
    monkeypatch.setattr(
        "hindsight_api.engine.bank_aliases.resolve", AsyncMock(side_effect=lambda backend, bank_id: bank_id)
    )
    engine._require_bank_exists = AsyncMock()
    engine._raise_if_bank_deleted = AsyncMock()
    engine._get_backend = AsyncMock(return_value=engine._backend)
    engine._get_read_backend = AsyncMock(return_value=engine._read_backend)
    engine._get_bank_profile_authenticated = AsyncMock(
        return_value={
            "bank_id": "fixture",
            "name": "Caldera",
            "mission": "Answer from fixture facts",
            "disposition": {"skepticism": 3, "literalism": 3, "empathy": 3},
        }
    )
    engine.get_bank_freshness = AsyncMock(return_value={})
    engine.list_directives = AsyncMock(return_value=DirectivePage(items=[], total=0))
    engine.attachments_for_memories = AsyncMock(return_value={})

    async def stored_evidence(bank_id: str, facts: list[MemoryFact]) -> list[MemoryFact]:
        return facts

    engine._evidence_as_stored = stored_evidence
    engine._audit_logger = AuditLogger(lambda: object(), lambda: "fixture", enabled=True, allowed_actions=[])
    engine._llm_recorder = LLMTraceRecorder(lambda: object(), lambda: "fixture", enabled=True, allowed_scopes=[])
    recorders = tracing.CompositeSpanRecorder()
    recorders.register(tracing.LLMSpanRecorder(provider.get_tracer("private-read-fixture")))
    recorders.register(engine._llm_recorder)
    monkeypatch.setattr(tracing, "_composite_recorder", recorders)
    store = PostgresMemories({})
    monkeypatch.setattr(memories, "_memories", store)
    monkeypatch.setattr("hindsight_api.engine.search.retrieval._default_graph_retriever", None)
    retrievals: list[dict[str, Any]] = []

    async def retrieve(**kwargs: Any) -> dict[str, RecallArms]:
        retrievals.append(kwargs)
        # This is the retained fixture row at the external DB boundary, not a
        # fabricated engine return. Native assembly, fusion and scoring consume it.
        row = RetrievalResult(
            id=FACT_ID,
            text=FACT_TEXT,
            fact_type="world",
            similarity=0.99,
            entity_ids=[],
            attachment_ids=[],
            tags=["fixture:readable"],
        )
        return {
            fact_type: RecallArms(semantic=[row] if fact_type == "world" else []) for fact_type in kwargs["fact_types"]
        }

    store.recall_unified = retrieve
    monkeypatch.setattr(
        "hindsight_api.engine.memory_engine.embedding_utils.generate_embeddings_batch",
        AsyncMock(return_value=[[0.1, 0.2, 0.3]]),
    )
    harness = ReadHarness(engine, store, sink, exporter, config, retrievals)
    try:
        yield harness
    finally:
        await harness.drain()
        provider.shutdown()
        clear_config_cache()


def _body(operation: str, query: str) -> dict[str, Any]:
    body: dict[str, Any] = (
        {"query": query, "types": ["world"]}
        if operation == "recall"
        else {
            "query": query,
            "fact_types": ["world"],
            "exclude_mental_models": True,
        }
    )
    return body


def _url(operation: str, private: bool) -> str:
    leaf = f"private/{operation}" if private else ("memories/recall" if operation == "recall" else "reflect")
    return f"/v1/default/banks/fixture/{leaf}"


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["recall", "reflect"])
async def test_real_engine_private_and_enabled_public_sinks(read_harness: ReadHarness, operation: str, caplog):
    h = read_harness
    app = create_app(h.engine, initialize_memory=False)
    with caplog.at_level(logging.INFO):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://fixture") as client:
            public = await client.post(_url(operation, False), json=_body(operation, PUBLIC_QUERY), headers=HEADERS)
            assert public.status_code == 200, public.text
            await h.drain()
            assert h.engine._audit_logger._enabled and h.engine._llm_recorder._enabled
            assert h.sink.writes and PUBLIC_QUERY in h.sink.bytes().decode()
            assert PUBLIC_QUERY in h.span_text()
            before_file, before_spans, before_logs = h.sink.bytes(), h.span_text(), caplog.text
            private = await client.post(_url(operation, True), json=_body(operation, PRIVATE_QUERY), headers=HEADERS)
            assert private.status_code == 200, private.text
            assert private.headers["X-Hindsight-Private-Read"] == "1"
            assert private.headers["Cache-Control"] == "no-store"
            await h.drain()
            assert h.sink.bytes() == before_file
            assert h.span_text() == before_spans
            assert PRIVATE_QUERY not in caplog.text
            # Known evidence must actually have travelled through native retrieval.
            if operation == "recall":
                assert private.json()["results"][0]["text"] == FACT_TEXT
            else:
                assert private.json()["text"] == FACT_TEXT
                assert any("llm_requests" in write.sql for write in h.sink.writes)
                assert any(
                    "hindsight.reflect_tool_exec.recall" == span.name for span in h.exporter.get_finished_spans()
                )
            assert len(h.retrievals) >= 2
            assert caplog.text.startswith(before_logs)
            restored = await client.post(
                _url("recall", False), json=_body("recall", "restored-public-query"), headers=HEADERS
            )
            assert restored.status_code == 200, restored.text
            await h.drain()
            assert "restored-public-query" in h.sink.bytes().decode()
    assert not is_private_read()


@pytest.mark.asyncio
async def test_real_private_auth_validation_and_provider_failure_restore_public(read_harness: ReadHarness, caplog):
    h = read_harness
    app = create_app(h.engine, initialize_memory=False)
    with caplog.at_level(logging.INFO):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://fixture") as client:
            malformed = await client.post(
                _url("recall", True), json={"query": PRIVATE_QUERY, "max_tokens": "invalid-sentinel"}, headers=HEADERS
            )
            assert malformed.status_code == 422
            unauthorized = await client.post(_url("recall", True), json=_body("recall", PRIVATE_QUERY))
            assert unauthorized.status_code == 401
            h.engine._reflect_llm_config._provider_impl.set_mock_exception(
                RuntimeError("provider-error-with-private-content")
            )
            failed = await client.post(_url("reflect", True), json=_body("reflect", PRIVATE_QUERY), headers=HEADERS)
            assert failed.status_code == 500, failed.text
            await h.drain()
            assert not h.sink.writes
            assert not h.exporter.get_finished_spans()
            assert PRIVATE_QUERY not in caplog.text
            assert "provider-error-with-private-content" not in caplog.text
            # Positive error control traverses the same provider/recorder owners.
            public = await client.post(_url("reflect", False), json=_body("reflect", PUBLIC_QUERY), headers=HEADERS)
            assert public.status_code == 500
            await h.drain()
            assert PUBLIC_QUERY in h.sink.bytes().decode()
            assert "provider-error-with-private-content" in h.sink.bytes().decode()
    assert not is_private_read()


@pytest.mark.asyncio
async def test_real_read_concurrency_and_cancelled_request(read_harness: ReadHarness, caplog):
    h = read_harness
    app = create_app(h.engine, initialize_memory=False)
    entered = asyncio.Event()
    release = asyncio.Event()
    native_db_seam = h.store.recall_unified

    async def blocked_retrieval(**kwargs: Any) -> dict[str, RecallArms]:
        if is_private_read():
            entered.set()
            await release.wait()
        return await native_db_seam(**kwargs)

    h.store.recall_unified = blocked_retrieval
    with caplog.at_level(logging.INFO):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://fixture") as client:
            task = asyncio.create_task(
                client.post(_url("recall", True), json=_body("recall", PRIVATE_QUERY), headers=HEADERS)
            )
            try:
                await asyncio.wait_for(entered.wait(), 5)
                public = await client.post(_url("recall", False), json=_body("recall", PUBLIC_QUERY), headers=HEADERS)
                assert public.status_code == 200, public.text
                await h.drain()
                assert PUBLIC_QUERY in h.sink.bytes().decode()
                public_file, public_spans = h.sink.bytes(), h.span_text()
                release.set()
                private = await task
                assert private.status_code == 200, private.text
                await h.drain()
                assert h.sink.bytes() == public_file
                assert h.span_text() == public_spans
                entered.clear()
                release.clear()
                task = asyncio.create_task(
                    client.post(_url("recall", True), json=_body("recall", PRIVATE_QUERY), headers=HEADERS)
                )
                await asyncio.wait_for(entered.wait(), 5)
                task.cancel(PRIVATE_QUERY)
                with pytest.raises(asyncio.CancelledError) as cancelled:
                    await task
                assert PRIVATE_QUERY not in str(cancelled.value)
                await h.drain()
                assert h.sink.bytes() == public_file
                assert h.span_text() == public_spans
                assert PRIVATE_QUERY not in caplog.text
            finally:
                release.set()
                if not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
    assert not is_private_read()


@pytest.mark.asyncio
async def test_real_public_validator_scope_and_private_fail_closed(read_harness: ReadHarness):
    from hindsight_api.engine.search.tags import TagGroupLeaf
    from hindsight_api.extensions.operation_validator import ValidationResult

    h = read_harness
    validator = AsyncMock()
    validator.precheck.return_value = ValidationResult.accept()
    validator.validate_recall.return_value = ValidationResult.accept()
    validator.validate_bank_read.return_value = ValidationResult.accept()
    validator.resolve_tag_scope.return_value = [TagGroupLeaf(tags=["fixture:readable"], match="all_strict")]
    h.engine._operation_validator = validator
    app = create_app(h.engine, initialize_memory=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://fixture") as client:
        public = await client.post(_url("recall", False), json=_body("recall", PUBLIC_QUERY), headers=HEADERS)
        assert public.status_code == 200, public.text
        assert h.retrievals[-1]["tag_groups"] == validator.resolve_tag_scope.return_value
        calls = list(validator.mock_calls)
        private = await client.post(_url("recall", True), json=_body("recall", PRIVATE_QUERY), headers=HEADERS)
        assert private.status_code == 503
        assert validator.mock_calls == calls  # never bypass it or hand it private input
        assert "X-Hindsight-Private-Read" not in private.headers
        validator.validate_recall.return_value = ValidationResult.reject("fixture-policy-denied", status_code=403)
        denied = await client.post(_url("recall", False), json=_body("recall", PUBLIC_QUERY), headers=HEADERS)
        assert denied.status_code == 403
    await h.drain()


@pytest.mark.asyncio
async def test_private_opaque_tenant_and_http_extension_reject_before_execution(read_harness: ReadHarness):
    from hindsight_api.api.private_reads import PrivateReadMiddleware

    h = read_harness

    class OpaqueTenant(ApiKeyTenantExtension):
        pass

    tenant = OpaqueTenant({"api_key": "fixture-only-key"})
    authenticated = AsyncMock(wraps=tenant.authenticate)
    tenant.authenticate = authenticated
    h.engine._tenant_extension = tenant
    app = create_app(h.engine, initialize_memory=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://fixture") as client:
        private = await client.post(_url("recall", True), content=PRIVATE_QUERY, headers=HEADERS)
        assert private.status_code == 503
        authenticated.assert_not_awaited()
        public = await client.post(_url("recall", False), json=_body("recall", PUBLIC_QUERY), headers=HEADERS)
        assert public.status_code == 200, public.text
        authenticated.assert_awaited()
    opaque_app = AsyncMock()
    guarded = PrivateReadMiddleware(opaque_app, h.engine, http_extension=True)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=guarded), base_url="http://fixture") as client:
        rejected = await client.post(_url("recall", True), content=PRIVATE_QUERY)
    assert rejected.status_code == 503
    opaque_app.assert_not_awaited()


def test_missing_capability_does_not_advertise_private_reads():
    # Generated clients use this schema default when reading older servers.
    legacy_features = {name: False for name in FeaturesInfo.model_fields if name != "private_reads"}
    assert FeaturesInfo.model_validate(legacy_features).private_reads is False


def test_openapi_private_routes_reuse_public_models_and_dependencies(read_harness: ReadHarness):
    from fastapi.routing import APIRoute

    app = create_app(read_harness.engine, initialize_memory=False)
    routes = {route.path: route for route in app.routes if isinstance(route, APIRoute)}
    schema = app.openapi()
    for operation in ("recall", "reflect"):
        private = routes[_url(operation, True).replace("fixture", "{bank_id}")]
        public = routes[_url(operation, False).replace("fixture", "{bank_id}")]
        assert private.endpoint is public.endpoint
        assert private.response_model is public.response_model
        assert [dep.call for dep in private.dependant.dependencies] == [
            dep.call for dep in public.dependant.dependencies
        ]
        assert "X-Hindsight-Private-Read" in schema["paths"][private.path]["post"]["responses"]["200"]["headers"]
    assert schema["components"]["schemas"]["FeaturesInfo"]["properties"]["private_reads"]["type"] == "boolean"


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["recall", "reflect"])
@pytest.mark.parametrize("excluded_urls", [None, "custom-public-exclusion"])
async def test_native_http_tracing_keeps_public_spans_and_excludes_private_reads(
    read_harness: ReadHarness, monkeypatch: pytest.MonkeyPatch, operation: str, excluded_urls: str | None
):
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

    h = read_harness
    # Instrument the real ASGI stack, not only the engine tracer. The transport
    # does not enter lifespan, so this never constructs or contacts an exporter.
    monkeypatch.setattr(h.config, "otel_exporter_otlp_endpoint", "http://fixture.invalid:4318")
    if excluded_urls is None:
        monkeypatch.delenv("OTEL_PYTHON_FASTAPI_EXCLUDED_URLS", raising=False)
    else:
        monkeypatch.setenv("OTEL_PYTHON_FASTAPI_EXCLUDED_URLS", excluded_urls)
    app = create_app(h.engine, initialize_memory=False)
    try:
        assert app._is_instrumented_by_opentelemetry
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://fixture") as client:
            public = await client.post(_url("recall", False), json=_body("recall", PUBLIC_QUERY), headers=HEADERS)
            assert public.status_code == 200, public.text
            await h.drain()
            public_server_count = sum(span.kind is trace.SpanKind.SERVER for span in h.exporter.get_finished_spans())
            assert public_server_count == 1
            public_spans = h.span_text()
            public_sink = h.sink.bytes()
            private = await client.post(_url(operation, True), json=_body(operation, PRIVATE_QUERY), headers=HEADERS)
            assert private.status_code == 200, private.text
            assert private.headers["X-Hindsight-Private-Read"] == "1"
            await h.drain()
            assert h.span_text() == public_spans
            assert h.sink.bytes() == public_sink
            restored = await client.post(
                _url("recall", False), json=_body("recall", "restored-native-http-query"), headers=HEADERS
            )
            assert restored.status_code == 200, restored.text
            await h.drain()
            assert (
                sum(span.kind is trace.SpanKind.SERVER for span in h.exporter.get_finished_spans())
                == public_server_count + 1
            )
            assert "restored-native-http-query" in h.sink.bytes().decode()
    finally:
        FastAPIInstrumentor.uninstrument_app(app)
