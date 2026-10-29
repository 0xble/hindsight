"""Producer, queue, exporter and worker controls for task-local no-store."""

from __future__ import annotations

import asyncio
import inspect
import logging
import logging.handlers
import queue
import threading
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from datetime import datetime, timezone
from unittest.mock import AsyncMock, Mock

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from hindsight_api import tracing
from hindsight_api.engine.audit import AuditEntry, AuditLogger
from hindsight_api.engine.background_writes import PendingWrites
from hindsight_api.engine.llm_trace import LLMRequestRecord, LLMTraceContext, LLMTraceRecorder, _BufferedRow
from hindsight_api.private_reads import (
    PrivateReadUnsupportedError,
    is_private_read,
    private_read_scope,
    run_in_executor,
)

QUERY = "private-sink-query-e31f"


class Unformattable:
    def __str__(self) -> str:
        raise AssertionError("private material was formatted")


@pytest.mark.asyncio
async def test_audit_and_llm_write_owners_never_resolve_serialize_or_schedule_private(monkeypatch):
    pool = Mock(side_effect=AssertionError("private DB lookup"))
    resolver = AsyncMock(side_effect=AssertionError("private audit config resolution"))
    audit = AuditLogger(pool, lambda: "fixture", enabled=True, allowed_actions=[], bank_enabled_resolver=resolver)
    llm = LLMTraceRecorder(pool, lambda: "fixture", enabled=True, allowed_scopes=[])
    now = datetime.now(timezone.utc)
    audit_entry = AuditEntry(action="reflect", transport="http", bank_id="fixture", request={"query": QUERY})
    llm_entry = LLMRequestRecord(
        provider="mock",
        model="fixture",
        scope="reflect",
        status="ok",
        started_at=now,
        ended_at=now,
        input=Unformattable(),
    )
    audit_schedule = Mock(side_effect=AssertionError("private audit scheduled"))
    llm_schedule = Mock(side_effect=AssertionError("private LLM trace scheduled"))
    monkeypatch.setattr(audit._writes, "schedule", audit_schedule)
    monkeypatch.setattr(llm._writes, "schedule", llm_schedule)
    with private_read_scope():
        assert not audit.action_allowed("reflect")
        assert not await audit.should_log("reflect", "fixture")
        audit.log_fire_and_forget(audit_entry)
        await audit._safe_log(audit_entry)
        assert not llm.is_enabled("reflect")
        llm.record_llm_call(provider="mock", model="fixture", scope="reflect", messages=Unformattable())
        llm._record_fire_and_forget(llm_entry)
        await llm._write_batch([_BufferedRow("fixture", llm_entry)])
        llm.attach_memory_ids(LLMTraceContext(bank_id="fixture", trace_id="fixture-trace"), created=[QUERY])
        await llm._attach_memory_ids("fixture", "fixture-trace", {"source_memory_ids": [QUERY]})
    pool.assert_not_called()
    # The released recorder buffers rows: a private call must not allocate one or
    # arm the flush timer, not merely skip the eventual write.
    assert llm._buffer == [] and not llm._flush_scheduled and not llm._rows_written
    resolver.assert_not_awaited()
    audit_schedule.assert_not_called()
    llm_schedule.assert_not_called()
    assert audit._enabled and llm._enabled
    # The public resolver still runs, rather than being globally disabled.
    resolver.side_effect = None
    resolver.return_value = True
    assert await audit.should_log("reflect", "fixture")
    resolver.assert_awaited_once()


@pytest.mark.asyncio
async def test_http_audit_wrapper_does_not_get_logger_or_capture_private_payload():
    from hindsight_api.api.http import _make_audited_http

    getter = Mock(side_effect=AssertionError("audit getter invoked"))
    capture = Mock(side_effect=AssertionError("private payload captured"))

    class Payload:
        model_dump = capture
        model_dump_json = capture

    @_make_audited_http(getter)("recall")
    async def read(request):
        return request

    payload = Payload()
    with private_read_scope():
        assert await read(request=payload) is payload
    getter.assert_not_called()
    capture.assert_not_called()
    with pytest.raises(AssertionError, match="audit getter invoked"):
        await read(request=payload)


@pytest.mark.asyncio
async def test_pending_write_suppression_closes_coroutine_before_task_creation(monkeypatch):
    executed: list[str] = []
    writes = PendingWrites(what="fixture")

    async def write() -> None:
        executed.append("ran")

    real_create_task = asyncio.create_task
    create = Mock(wraps=real_create_task)
    monkeypatch.setattr(asyncio, "create_task", create)
    private_write = write()
    with private_read_scope():
        assert writes.schedule(private_write) is False
    create.assert_not_called()
    assert inspect.getcoroutinestate(private_write) == inspect.CORO_CLOSED
    assert executed == []
    assert writes.schedule(write()) is True
    await writes.drain_all(5)
    assert executed == ["ran"]


def test_logging_producer_stops_record_creation_formatting_and_queue_preparation(monkeypatch, tmp_path):
    records: queue.Queue = queue.Queue()
    handler = logging.handlers.QueueHandler(records)
    log_path = tmp_path / "producer.log"
    file_handler = logging.FileHandler(log_path, delay=True)
    prepared = Mock(wraps=handler.prepare)
    monkeypatch.setattr(handler, "prepare", prepared)
    factory = Mock(wraps=logging.getLogRecordFactory())
    monkeypatch.setattr(logging, "_logRecordFactory", factory)
    logger = logging.getLogger("hindsight.private-fixture.queue")
    old_handlers, old_level, old_propagate = logger.handlers[:], logger.level, logger.propagate
    logger.handlers, logger.propagate = [handler, file_handler], False
    logger.setLevel(logging.INFO)
    try:
        with private_read_scope():
            logger.info("query %s", Unformattable())
            logger.error(Unformattable(), exc_info=True)
            logger.handle(logging.LogRecord(logger.name, logging.ERROR, __file__, 1, Unformattable(), (), None))
        factory.assert_not_called()
        prepared.assert_not_called()
        assert records.empty()
        assert not log_path.exists()
        logger.info("public restored %s", "fixture")
        assert records.get_nowait().getMessage() == "public restored fixture"
        assert prepared.call_count == 1
        assert factory.call_count == 1
        file_handler.flush()
        assert log_path.read_text().strip() == "public restored fixture"
    finally:
        logger.handlers, logger.propagate = old_handlers, old_propagate
        logger.setLevel(old_level)
        handler.close()
        file_handler.close()


def test_real_otel_export_owner_composite_and_inherited_span_are_private(monkeypatch):
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("private-sink-fixture")
    recorder = tracing.LLMSpanRecorder(tracer)
    composite = tracing.CompositeSpanRecorder()
    composite.register(recorder)
    opaque_recorder = Mock()
    composite.register(opaque_recorder)
    monkeypatch.setattr(tracing, "_tracer", tracer)
    monkeypatch.setattr(tracing, "_tracing_enabled", True)
    try:
        with tracer.start_as_current_span("public-parent") as parent:
            with private_read_scope():
                assert not tracing.is_tracing_enabled()
                trace.get_current_span().add_event(QUERY)
                with tracing.get_tracer().start_as_current_span(QUERY) as private_span:
                    private_span.set_attribute("query", QUERY)
                    private_span.record_exception(RuntimeError(QUERY))
                with tracing.create_operation_span(QUERY):
                    pass
                recorder.record_llm_call(
                    provider="mock",
                    model="fixture",
                    scope="reflect",
                    messages=Unformattable(),
                    response_content=None,
                    input_tokens=0,
                    output_tokens=0,
                    duration=0.0,
                    error=Unformattable(),
                )
                composite.record_llm_call(messages=Unformattable())
                payload = {}
                tracing.inject_task_trace_context(payload)
                assert payload == {}
            parent.add_event("public-restored-event")
        assert [span.name for span in exporter.get_finished_spans()] == ["public-parent"]
        assert [event.name for event in exporter.get_finished_spans()[0].events] == ["public-restored-event"]
        opaque_recorder.record_llm_call.assert_not_called()
        recorder.record_llm_call(
            provider="mock",
            model="fixture",
            scope="reflect",
            messages=[{"role": "user", "content": "public-prompt"}],
            response_content="public-answer",
            input_tokens=1,
            output_tokens=1,
            duration=0.01,
        )
        assert "public-prompt" in "\n".join(span.to_json() for span in exporter.get_finished_spans())
    finally:
        provider.shutdown()


@pytest.mark.asyncio
async def test_real_temporal_worker_preserves_scope_and_public_error_logging(monkeypatch, caplog):
    from hindsight_api.engine.query_analyzer import DateparserQueryAnalyzer
    from hindsight_api.engine.search import temporal_extraction

    seen: list[bool] = []
    analyzer = DateparserQueryAnalyzer(languages=["en"])

    def analyze(query: str, reference_date=None):
        seen.append(is_private_read())
        raise ValueError(query)

    monkeypatch.setattr(analyzer, "analyze", analyze)
    with ThreadPoolExecutor(max_workers=1) as executor:
        monkeypatch.setattr(temporal_extraction, "_executor", executor)
        with caplog.at_level(logging.WARNING):
            with private_read_scope():
                assert await temporal_extraction.extract_temporal_constraint_async(QUERY, analyzer=analyzer) is None
            assert QUERY not in caplog.text
            assert (
                await temporal_extraction.extract_temporal_constraint_async("public-temporal-error", analyzer=analyzer)
                is None
            )
            assert "public-temporal-error" in caplog.text
    assert seen == [True, False]


@pytest.mark.asyncio
async def test_real_reranker_thread_keeps_scope_after_request_cancellation(monkeypatch, caplog):
    from hindsight_api.engine.cross_encoder import FlashRankCrossEncoder

    entered = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    observations: list[bool] = []
    encoder = FlashRankCrossEncoder.__new__(FlashRankCrossEncoder)
    encoder._ranker = object()

    def predict(pairs, cancelled=None):
        entered.set()
        try:
            if not release.wait(5):
                raise AssertionError("fixture did not release worker")
            observations.append(is_private_read())
            logging.getLogger("hindsight.private-worker").error("late worker %s", pairs[0][0])
            return [0.9]
        finally:
            finished.set()

    monkeypatch.setattr(encoder, "_predict_sync", predict)

    async def private_request() -> None:
        with private_read_scope():
            await encoder._predict([(QUERY, "fixture-document")])

    with ThreadPoolExecutor(max_workers=1) as executor:
        monkeypatch.setattr(FlashRankCrossEncoder, "_executor", executor)
        with caplog.at_level(logging.INFO):
            task = asyncio.create_task(private_request())
            try:
                assert await asyncio.to_thread(entered.wait, 5)
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
                assert not is_private_read()
                logging.getLogger("hindsight.private-worker").info("public-after-cancellation")
                release.set()
                assert await asyncio.to_thread(finished.wait, 5)
                assert observations == [True]
                assert QUERY not in caplog.text
                assert "public-after-cancellation" in caplog.text
            finally:
                release.set()
                await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["reranker", "failover"])
async def test_native_model_initialization_workers_inherit_policy(monkeypatch, boundary):
    from hindsight_api.engine.cross_encoder import LocalSTCrossEncoder, MultiCrossEncoder
    from hindsight_api.engine.search.reranking import CrossEncoderReranker

    seen: list[bool] = []
    encoder = LocalSTCrossEncoder.__new__(LocalSTCrossEncoder)

    async def initialize():
        seen.append(is_private_read())

    monkeypatch.setattr(encoder, "initialize", initialize)
    if boundary == "reranker":
        owner = CrossEncoderReranker(encoder)
        with private_read_scope():
            await owner.ensure_initialized()
        owner._initialized = False
        await owner.ensure_initialized()
    else:
        owner = MultiCrossEncoder.__new__(MultiCrossEncoder)
        owner._members = [encoder]
        owner._ready = [False]
        with private_read_scope():
            await owner._initialize_member(0)
        await owner._initialize_member(0)
    assert seen == [True, False]


def test_private_opaque_store_and_graph_retriever_are_never_invoked(monkeypatch):
    from hindsight_api.engine import memories
    from hindsight_api.engine.search import retrieval

    opaque = Mock()
    monkeypatch.setattr(memories, "_memories", opaque)
    monkeypatch.setattr(retrieval, "_default_graph_retriever", opaque)
    with private_read_scope():
        with pytest.raises(PrivateReadUnsupportedError):
            memories.get_memories()
        with pytest.raises(PrivateReadUnsupportedError):
            retrieval.get_default_graph_retriever()
    assert opaque.mock_calls == []
    assert memories.get_memories() is opaque
    assert retrieval.get_default_graph_retriever() is opaque


@pytest.mark.asyncio
async def test_private_executor_rejects_process_before_submission():
    # __new__ avoids ever creating a subprocess. The policy must reject before
    # touching an executor method, not after attempting to pickle private args.
    executor = ProcessPoolExecutor.__new__(ProcessPoolExecutor)
    with private_read_scope(), pytest.raises(PrivateReadUnsupportedError):
        await run_in_executor(executor, str, QUERY)


def test_private_trace_attribution_does_not_mutate_public_parent():
    from hindsight_api.engine import llm_trace

    parent = LLMTraceContext(bank_id="fixture", trace_id="public-parent")
    token = llm_trace.set_trace_context(parent)
    try:
        with private_read_scope():
            assert llm_trace.current_trace_context() is None
            llm_trace.record_source_memory_ids([QUERY])
            llm_trace.record_created_memory_ids([QUERY])
        assert llm_trace.current_trace_context() is parent
        assert parent.source_memory_ids == []
        assert parent.created_memory_ids == []
        llm_trace.record_source_memory_ids(["public-source"])
        assert parent.source_memory_ids == ["public-source"]
    finally:
        llm_trace.reset_trace_context(token)


def test_private_path_policy_covers_decoded_control_characters_and_mounts():
    import re

    from hindsight_api.api.private_reads import PRIVATE_READ_PATH, PRIVATE_READ_TRACE_EXCLUSION

    for bank in ("fixture", "line\nbreak", "bank?name"):
        for prefix in ("", "/mount", "/mount\nwith-control"):
            for operation in ("recall", "reflect"):
                for suffix in ("", "/"):
                    path = f"{prefix}/v1/default/banks/{bank}/private/{operation}{suffix}"
                    assert PRIVATE_READ_PATH.search(path)
                    assert re.search(PRIVATE_READ_TRACE_EXCLUSION, f"http://fixture{path}?extra=1")
    assert PRIVATE_READ_PATH.search("/v1/default/banks/fixture/memories/recall") is None
    assert (
        re.search(PRIVATE_READ_TRACE_EXCLUSION, "http://fixture/version?next=/v1/default/banks/fixture/private/recall")
        is None
    )


@pytest.mark.parametrize(
    "path",
    [
        "/v1/default/banks/b/documents/evil/private/recall",
        "/v1/default/banks/b/documents/evil/private/reflect",
        "/mount/v1/default/banks/b/documents/evil/private/recall/",
        "/v1/default/banks/b/documents/v1/default/banks/x/private/recall",
        "/v1/default/banks/b/documents/v1/default/banks/x/private/reflect",
        "/v1/default/banks//private/recall",
        "/v1/default/banks/b/private/recall/extra",
        "/v1/default/banks/b/private/recall\n",
    ],
)
def test_private_path_policy_does_not_reclassify_public_or_malformed_paths(path: str) -> None:
    import re

    from hindsight_api.api.private_reads import PRIVATE_READ_PATH, PRIVATE_READ_TRACE_EXCLUSION

    assert PRIVATE_READ_PATH.search(path) is None
    assert re.search(PRIVATE_READ_TRACE_EXCLUSION, f"http://fixture{path}") is None


def test_private_native_sdk_alias_is_rejected_before_provider_access():
    from hindsight_api.engine.llm_wrapper import LLMProvider
    from hindsight_api.engine.private_reads import check_private_llm_config

    config = LLMProvider.__new__(LLMProvider)
    config.provider = "codex"
    config._provider_impl = Mock()
    with private_read_scope(), pytest.raises(PrivateReadUnsupportedError):
        check_private_llm_config(config)
    assert config._provider_impl.mock_calls == []


def test_private_provider_factory_rejects_opaque_subprocess_before_construction(monkeypatch):
    from hindsight_api.engine import providers
    from hindsight_api.engine.llm_wrapper import create_llm_provider

    constructor = Mock(side_effect=AssertionError("subprocess provider constructed"))
    monkeypatch.setattr(providers, "ClaudeCodeLLM", constructor)
    with private_read_scope(), pytest.raises(PrivateReadUnsupportedError):
        create_llm_provider(provider="claude-code", api_key="", base_url="", model="fixture", reasoning_effort=None)
    constructor.assert_not_called()
