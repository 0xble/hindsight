"""Transport outages must not settle file conversion as a deterministic failure."""

import asyncio
import ssl
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from markitdown import FileConversionException
from markitdown._exceptions import FailedConversionAttempt
from openai import APIConnectionError, APITimeoutError

from hindsight_api.engine.memory_engine import MemoryEngine
from hindsight_api.engine.parsers import NoExtractableContentError
from hindsight_api.worker.exceptions import RetryTaskAt


@pytest.fixture
def engine():
    engine = MemoryEngine.__new__(MemoryEngine)
    engine._audit_logger = None
    engine._get_backend = AsyncMock(side_effect=RuntimeError("no database in this unit test"))
    engine._handle_file_convert_retain = AsyncMock()
    engine._mark_operation_failed = AsyncMock()
    return engine


def _task(retry_count=0):
    return {
        "type": "file_convert_retain",
        "operation_id": str(uuid.uuid4()),
        "bank_id": "test-file-transport",
        "_retry_count": retry_count,
    }


def _connection_errors():
    request = httpx.Request("POST", "https://ocr.invalid/v1/chat/completions")
    return [
        APIConnectionError(request=request),
        APITimeoutError(request=request),
        httpx.ConnectError("connection failed"),
        httpx.ReadError("read failed"),
        httpx.ReadTimeout("read timed out"),
        httpx.RemoteProtocolError("server disconnected"),
        ssl.SSLError("[SSL: SSLV3_ALERT_BAD_RECORD_MAC]"),
    ]


@pytest.mark.parametrize("error", _connection_errors())
async def test_file_transport_failure_uses_worker_retry(engine, error):
    engine._handle_file_convert_retain.side_effect = error
    config = SimpleNamespace(worker_max_retries=3, worker_task_retry_backoff_seconds=17)
    before = datetime.now(UTC)
    with patch("hindsight_api.engine.memory_engine.get_config", return_value=config):
        with pytest.raises(RetryTaskAt) as caught:
            await engine._execute_task(_task())
    assert 17 <= (caught.value.retry_at - before).total_seconds() < 22
    engine._mark_operation_failed.assert_not_awaited()


@pytest.mark.parametrize("wrapped_in_attempt", [False, True])
async def test_wrapped_markitdown_transport_failure_retries(engine, wrapped_in_attempt):
    transport_error = _connection_errors()[0]
    if wrapped_in_attempt:
        # MarkItDown stores converter exceptions here rather than in __cause__.
        conversion_error = FileConversionException(
            attempts=[FailedConversionAttempt(object(), (type(transport_error), transport_error, None))]
        )
    else:
        conversion_error = FileConversionException("conversion failed")
        conversion_error.__cause__ = transport_error
    outer = RuntimeError("failed to parse image")
    outer.__cause__ = conversion_error
    engine._handle_file_convert_retain.side_effect = outer
    with pytest.raises(RetryTaskAt):
        await engine._execute_task(_task())
    engine._mark_operation_failed.assert_not_awaited()


@pytest.mark.parametrize(
    "error", [ValueError("corrupt PDF"), NoExtractableContentError("empty"), RuntimeError("unsupported format")]
)
async def test_deterministic_file_failure_stays_terminal(engine, error):
    engine._handle_file_convert_retain.side_effect = error
    task = _task()
    await engine._execute_task(task)
    engine._mark_operation_failed.assert_awaited_once()
    assert engine._mark_operation_failed.call_args.args[0] == task["operation_id"]


async def test_file_transport_retry_respects_worker_cap(engine):
    error = _connection_errors()[0]
    engine._handle_file_convert_retain.side_effect = error
    config = SimpleNamespace(worker_max_retries=2, worker_task_retry_backoff_seconds=17)
    with patch("hindsight_api.engine.memory_engine.get_config", return_value=config):
        with pytest.raises(APIConnectionError):
            await engine._execute_task(_task(retry_count=2))
    engine._mark_operation_failed.assert_not_awaited()


@pytest.mark.parametrize("caller", ["ocr", "pdf_ocr", "retain", "consolidation"])
async def test_openai_clients_recover_on_fresh_connections(caller):
    """Exercise real SDK/httpcore retries, not mocked completion methods."""
    from aiohttp import web

    from hindsight_api.engine.llm_wrapper import LLMProvider
    from hindsight_api.engine.parsers.markitdown import MarkitdownParser
    from tests.aiohttp_stub import stub_server

    connections = []

    async def handler(request):
        connections.append(request.transport)
        if len(connections) <= 3:
            request.transport.abort()
            return web.Response()
        return web.json_response(
            {
                "id": "test-completion",
                "object": "chat.completion",
                "created": 0,
                "model": "test-model",
                "choices": [
                    {"index": 0, "message": {"role": "assistant", "content": "hello"}, "finish_reason": "stop"}
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            }
        )

    async with stub_server(handler) as base_url:
        if caller == "ocr":
            parser = MarkitdownParser(
                ocr_enabled=True, ocr_api_key="test", ocr_base_url=base_url, ocr_model="test-model"
            )
            config = SimpleNamespace(llm_max_retries=3)
            with patch("hindsight_api.engine.parsers.markitdown.get_config", return_value=config):
                options = parser._build_ocr_options(
                    api_key="test", base_url=base_url, model="test-model", prompt=None, default_headers=None
                )
            client = options.llm_client
            try:
                result = await asyncio.to_thread(
                    client.chat.completions.create,
                    model="test-model",
                    messages=[{"role": "user", "content": "transcribe"}],
                )
                assert result.choices[0].message.content == "hello"
            finally:
                await asyncio.to_thread(client.close)
        elif caller == "pdf_ocr":
            import time

            from hindsight_api.engine.parsers.pdf_ocr import PdfOcrConfig, _DeadlineOcrClient

            client = _DeadlineOcrClient(
                PdfOcrConfig(api_key="test", base_url=base_url, model="test-model", prompt=None, default_headers=None),
                request_seconds=15,
                document_deadline=time.monotonic() + 20,
            )
            result = await asyncio.to_thread(
                client.create, model="test-model", messages=[{"role": "user", "content": "transcribe"}]
            )
            assert result.choices[0].message.content == "hello"
        else:
            # The wrapper's configured budget is also used by consolidation's
            # with_config provider. Keep SDK retries disabled to avoid multiplying
            # this budget or defeating the single-completion schema correction.
            provider = LLMProvider(
                provider="openai",
                api_key="test",
                base_url=base_url,
                model="test-model",
                max_retries=3,
                initial_backoff=0,
                max_backoff=0,
            )
            try:
                result = await provider.call(messages=[{"role": "user", "content": "hello"}], scope=caller)
                assert result.content == "hello"
            finally:
                await provider.cleanup()
    assert len(connections) == 4
    assert len({id(connection) for connection in connections}) == 4


def _pdf_transport_failure_worker(source, result_path, ocr_config, limits, deadline):
    """Inject a provider failure inside the real disposable-worker boundary."""
    from hindsight_api.engine.parsers import pdf_ocr

    def fail(*args, **kwargs):
        request = httpx.Request("POST", "https://ocr.invalid/v1/chat/completions")
        raise APIConnectionError(message="secret provider detail", request=request)

    pdf_ocr._convert_pages = fail
    pdf_ocr._worker(source, result_path, ocr_config, limits, deadline)


async def test_pdf_process_preserves_transport_classification_without_private_text(engine, monkeypatch):
    from hindsight_api.engine.parsers import pdf_ocr
    from hindsight_api.engine.parsers.transport import FileParserTransportError

    monkeypatch.setattr(pdf_ocr, "_worker", _pdf_transport_failure_worker)
    config = pdf_ocr.PdfOcrConfig(
        api_key="synthetic-key", base_url="https://ocr.invalid", model="test-model", prompt=None, default_headers=None
    )
    with pytest.raises(FileParserTransportError) as caught:
        await pdf_ocr.convert_pdf(b"stub source", "scan.pdf", config, pdf_ocr.PDF_OCR_LIMITS)
    assert "secret provider detail" not in str(caught.value)
    engine._handle_file_convert_retain.side_effect = caught.value
    with pytest.raises(RetryTaskAt):
        await engine._execute_task(_task())
    engine._mark_operation_failed.assert_not_awaited()


def test_classifier_terminates_on_cyclic_cause_and_does_not_match_error_prose():
    from hindsight_api.engine.parsers.transport import is_retryable_file_transport_error

    error = RuntimeError("APIConnectionError: SSLV3_ALERT_BAD_RECORD_MAC")
    error.__cause__ = error
    assert not is_retryable_file_transport_error(error)


@pytest.mark.parametrize("case", ["already_expired", "document_timeout", "request_timeout"])
async def test_pdf_document_deadline_is_terminal_but_request_timeout_retries(engine, case):
    import time

    from aiohttp import web
    from openai import AsyncOpenAI

    from hindsight_api.engine.parsers.pdf_ocr import PdfOcrConfig, _DeadlineOcrClient
    from hindsight_api.engine.parsers.transport import FileParserDeadlineError, FileParserRequestTimeout
    from tests.aiohttp_stub import stub_server

    # Exclude lazy SDK import latency from the elapsed deadline being tested.
    async with AsyncOpenAI(api_key="test") as warmup:
        _ = warmup.chat.completions

    async def handler(request):
        await asyncio.sleep(0.3)
        return web.Response()

    async with stub_server(handler) as base_url:
        document_seconds = {"already_expired": -1, "document_timeout": 0.1, "request_timeout": 10}[case]
        request_seconds = 0.05 if case == "request_timeout" else 5
        client = _DeadlineOcrClient(
            PdfOcrConfig(api_key="test", base_url=base_url, model="test-model", prompt=None, default_headers=None),
            request_seconds=request_seconds,
            document_deadline=time.monotonic() + document_seconds,
        )
        expected = FileParserRequestTimeout if case == "request_timeout" else FileParserDeadlineError
        with pytest.raises(expected) as caught:
            await asyncio.to_thread(client.create, model="test-model", messages=[{"role": "user", "content": "hello"}])
    engine._handle_file_convert_retain.side_effect = caught.value
    if case == "request_timeout":
        with pytest.raises(RetryTaskAt):
            await engine._execute_task(_task())
        engine._mark_operation_failed.assert_not_awaited()
    else:
        await engine._execute_task(_task())
        engine._mark_operation_failed.assert_awaited_once()


@pytest.mark.parametrize("wrapped", [False, True])
async def test_certificate_verification_failure_stays_terminal(engine, wrapped):
    error = ssl.SSLCertVerificationError("bad certificate")
    if wrapped:
        outer = _connection_errors()[0]
        outer.__cause__ = error
        error = outer
    engine._handle_file_convert_retain.side_effect = error
    await engine._execute_task(_task())
    engine._mark_operation_failed.assert_awaited_once()
