"""Total request deadlines survive non-streaming keepalive bytes.

The real SDK/HTTP path is exercised against a local server, not a mocked
completion. Related upstream fix: vectorize-io/hindsight#4784. This fork
preserves the SDK's APITimeoutError classification and existing retry handlers.
"""

import asyncio
import json
import time
from collections.abc import AsyncIterator, Awaitable
from contextlib import asynccontextmanager, suppress
from typing import TypeVar

import httpx
import pytest
from aiohttp import ClientResponse, StreamReader, web
from openai import APITimeoutError
from pydantic import BaseModel

from hindsight_api.engine.providers.openai_compatible_llm import OpenAICompatibleLLM
from tests.aiohttp_stub import stub_server

# A cold SDK can spend >300ms serializing before sending anything under xdist.
# Warm the same path, then leave room for scheduling while still bounding real HTTP.
TIMEOUT = 2.0
OBSERVATION_TIMEOUT = 20.0
_Result = TypeVar("_Result")
MESSAGES = [{"role": "user", "content": "hi"}]
TOOLS = [{"type": "function", "function": {"name": "noop", "parameters": {"type": "object", "properties": {}}}}]


class _Ok(BaseModel):
    ok: bool


def _provider(url, path, timeout=TIMEOUT):
    return OpenAICompatibleLLM(
        provider="ollama" if path == "native" else "openai",
        api_key="fake-local-key",
        base_url=f"{url}/v1",
        model="openai/gpt-oss-20b",
        timeout=timeout,
    )


async def _invoke(llm, path, **kwargs):
    if path == "tools":
        return await llm.call_with_tools(messages=MESSAGES, tools=TOOLS, **kwargs)
    return await llm.call(
        messages=MESSAGES,
        response_format=_Ok if path in ("structured", "native") else None,
        scope="consolidation_dedup",
        **kwargs,
    )


def _body(path):
    content = '{"ok": true}' if path in ("structured", "native") else "ok"
    if path == "native":
        return {"message": {"content": content}, "done": True, "done_reason": "stop"}
    return {
        "id": "fake-local",
        "object": "chat.completion",
        "created": 0,
        "model": "openai/gpt-oss-20b",
        "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": content}}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }


async def _within_observation(call: Awaitable[_Result]) -> _Result:
    """Fail explicitly if the safety net fires, even for native TimeoutError."""
    task = asyncio.ensure_future(call)
    try:
        done, _ = await asyncio.wait({task}, timeout=OBSERVATION_TIMEOUT)
        assert done, "provider did not finish before the independent observation ceiling"
        return await task
    finally:
        if not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task


async def _warm_up(llm: OpenAICompatibleLLM, path: str) -> None:
    # Exercise serialization, schema preparation, SDK initialization and real HTTP
    # once with a completed response, without spending the regression's deadline.
    llm.timeout = OBSERVATION_TIMEOUT
    try:
        await _within_observation(_invoke(llm, path, max_retries=0))
    finally:
        llm.timeout = TIMEOUT


def _observe_body_reads(monkeypatch: pytest.MonkeyPatch, path: str) -> list[bytes]:
    """Observe bytes delivered to the real client, not just server-side writes."""
    chunks: list[bytes] = []
    if path == "native":
        start = ClientResponse.start
        readany = StreamReader.readany
        client_readers: set[StreamReader] = set()

        async def observed_start(response: ClientResponse, *args, **kwargs) -> ClientResponse:
            result = await start(response, *args, **kwargs)
            # The server uses StreamReader for request.json() too; register only
            # response readers so those request bytes cannot satisfy the test.
            client_readers.add(response.content)
            return result

        async def observed_readany(reader: StreamReader) -> bytes:
            chunk = await readany(reader)
            if reader in client_readers and chunk:
                chunks.append(chunk)
            return chunk

        # StreamReader instances are slotted, so observe the class method while
        # restricting the evidence to the registered client-side instances.
        monkeypatch.setattr(ClientResponse, "start", observed_start)
        monkeypatch.setattr(StreamReader, "readany", observed_readany)
    else:
        aiter_raw = httpx.Response.aiter_raw

        async def observed_aiter_raw(response: httpx.Response, *args, **kwargs) -> AsyncIterator[bytes]:
            async for chunk in aiter_raw(response, *args, **kwargs):
                chunks.append(chunk)
                yield chunk

        monkeypatch.setattr(httpx.Response, "aiter_raw", observed_aiter_raw)
    return chunks


def _handler(requests, path, *, success_after=None, warmup=False):
    warmed = False

    async def handle(request):
        nonlocal warmed
        payload = await request.json()
        if warmup and not warmed:
            warmed = True
            return web.json_response(_body(path))
        requests.append(payload)
        if success_after is not None and len(requests) > success_after:
            return web.json_response(_body(path))
        response = web.StreamResponse(headers={"Content-Type": "application/json"})
        await response.prepare(request)
        try:
            while True:
                await response.write(b" \n")
                await asyncio.sleep(0.02)  # below the SDK's per-read timeout
        except (ConnectionResetError, asyncio.CancelledError):
            pass
        return response

    return handle


@pytest.mark.parametrize("path", ["free", "structured", "tools", "native"])
@pytest.mark.parametrize("retries", [0, 1])
async def test_keepalive_deadline_preserves_timeout_type_and_retry_count(path, retries, monkeypatch):
    requests = []
    admitted = []

    @asynccontextmanager
    async def admission():
        admitted.append(time.monotonic())
        yield

    async with stub_server(_handler(requests, path, warmup=True)) as url:
        llm = _provider(url, path)
        try:
            await _warm_up(llm, path)
            chunks = _observe_body_reads(monkeypatch, path)
            start = time.monotonic()
            expected = TimeoutError if path == "native" else APITimeoutError
            with pytest.raises(expected) as raised:
                await _within_observation(
                    _invoke(llm, path, max_retries=retries, initial_backoff=0.01, attempt_context=admission)
                )
            elapsed = time.monotonic() - start
            assert elapsed >= TIMEOUT * (retries + 1)
            # Admission is the retry contract. Under contention, an admitted
            # attempt can expire before HTTP reaches the server.
            assert len(admitted) == retries + 1
            assert 0 < len(requests) <= len(admitted)
            assert len(chunks) >= 2 and all(chunk.isspace() for chunk in chunks)
            assert all(not payload.get("stream", False) for payload in requests)
            if path != "native":
                assert isinstance(raised.value.__cause__, TimeoutError)
                assert raised.value.request.url.path == "/v1/chat/completions"
        finally:
            await llm.cleanup()


@pytest.mark.parametrize("path", ["free", "structured", "tools", "native"])
async def test_timeout_retry_gets_a_fresh_deadline_and_can_succeed(path, monkeypatch):
    requests = []
    admitted = []

    @asynccontextmanager
    async def admission():
        admitted.append(time.monotonic())
        yield

    async with stub_server(_handler(requests, path, success_after=1, warmup=True)) as url:
        llm = _provider(url, path)
        try:
            await _warm_up(llm, path)
            chunks = _observe_body_reads(monkeypatch, path)
            result = await _within_observation(
                _invoke(llm, path, max_retries=1, initial_backoff=0.01, attempt_context=admission)
            )
            assert len(admitted) == 2
            assert len(requests) == 2
            assert sum(chunk.isspace() for chunk in chunks) >= 2
            assert result.content == (_Ok(ok=True) if path in ("structured", "native") else "ok")
        finally:
            await llm.cleanup()


@pytest.mark.parametrize("path", ["free", "structured", "tools", "native"])
async def test_successful_response_within_deadline_is_unchanged(path):
    async def handle(request):
        await request.json()
        # Leading whitespace is legal in a non-streaming JSON response.
        return web.Response(text=" \n" + json.dumps(_body(path)), content_type="application/json")

    async with stub_server(handle) as url:
        llm = _provider(url, path)
        try:
            result = await _invoke(llm, path, max_retries=0)
            assert result.content == (_Ok(ok=True) if path in ("structured", "native") else "ok")
        finally:
            await llm._client.close()


async def test_attempt_admission_wait_is_not_part_of_request_deadline():
    @asynccontextmanager
    async def admission():
        await asyncio.sleep(TIMEOUT * 2)
        yield

    requests = []
    async with stub_server(_handler(requests, "free", success_after=0)) as url:
        llm = _provider(url, "free")
        try:
            result = await _invoke(llm, "free", max_retries=0, attempt_context=admission)
            assert result.content == "ok"
            assert len(requests) == 1
        finally:
            await llm._client.close()


async def test_dedup_resolves_consolidation_timeout_not_global(monkeypatch):
    from hindsight_api import MemoryEngine
    from hindsight_api.config import _get_raw_config, clear_config_cache

    monkeypatch.setenv("HINDSIGHT_API_LLM_PROVIDER", "mock")
    monkeypatch.setenv("HINDSIGHT_API_LLM_TIMEOUT", "120")
    monkeypatch.setenv("HINDSIGHT_API_CONSOLIDATION_LLM_PROVIDER", "openai")
    monkeypatch.setenv("HINDSIGHT_API_CONSOLIDATION_LLM_API_KEY", "fake-local-key")
    monkeypatch.setenv("HINDSIGHT_API_CONSOLIDATION_LLM_MODEL", "openai/gpt-oss-20b")
    requests = []
    async with stub_server(_handler(requests, "structured", success_after=0)) as url:
        monkeypatch.setenv("HINDSIGHT_API_CONSOLIDATION_LLM_BASE_URL", f"{url}/v1")
        monkeypatch.setenv("HINDSIGHT_API_CONSOLIDATION_LLM_TIMEOUT", "300")
        clear_config_cache()
        engine = MemoryEngine(skip_llm_verification=True)
        config = _get_raw_config()
        dedup = engine._consolidation_llm_config.with_config(
            config, bank_id="local-test", operation="consolidation_dedup"
        )
        try:
            assert engine._llm_config.timeout == 120
            assert dedup.timeout == 300
            assert dedup._provider_impl.timeout == 300
            assert dedup._provider_impl._client.timeout.read == 300
            result = await dedup.call(
                messages=MESSAGES, response_format=_Ok, scope="consolidation_dedup", max_retries=0
            )
            assert result.content == _Ok(ok=True)
            assert len(requests) == 1
        finally:
            await dedup._provider_impl._client.close()
            await engine.close()


async def test_external_cancellation_is_not_converted_to_timeout_or_retried():
    requests = []
    async with stub_server(_handler(requests, "free")) as url:
        llm = _provider(url, "free", timeout=5)
        task = asyncio.create_task(_invoke(llm, "free", max_retries=2))
        try:
            async with asyncio.timeout(1):
                while not requests:
                    await asyncio.sleep(0.01)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert len(requests) == 1
        finally:
            task.cancel()
            await llm._client.close()
