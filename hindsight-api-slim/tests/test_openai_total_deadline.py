"""Total request deadlines survive non-streaming keepalive bytes.

The real SDK/HTTP path is exercised against a local server, not a mocked
completion. Related upstream fix: vectorize-io/hindsight#4784. This fork
preserves the SDK's APITimeoutError classification and existing retry handlers.
"""

import asyncio
import json
import time
from contextlib import asynccontextmanager

import pytest
from aiohttp import web
from openai import APITimeoutError
from pydantic import BaseModel

from hindsight_api.engine.providers.openai_compatible_llm import OpenAICompatibleLLM
from tests.aiohttp_stub import stub_server

TIMEOUT = 0.3
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


def _handler(requests, path, *, success_after=None):
    async def handle(request):
        requests.append(await request.json())
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
async def test_keepalive_deadline_preserves_timeout_type_and_retry_count(path, retries):
    requests = []
    async with stub_server(_handler(requests, path)) as url:
        llm = _provider(url, path)
        start = time.monotonic()
        try:
            expected = TimeoutError if path == "native" else APITimeoutError
            with pytest.raises(expected) as raised:
                # Independent safety net: pre-fix SDK calls hang until this fires.
                await asyncio.wait_for(_invoke(llm, path, max_retries=retries, initial_backoff=0.01), 2)
            elapsed = time.monotonic() - start
            assert TIMEOUT * (retries + 1) <= elapsed < 1.5
            assert len(requests) == retries + 1
            assert all(not payload.get("stream", False) for payload in requests)
            if path != "native":
                assert isinstance(raised.value.__cause__, TimeoutError)
                assert raised.value.request.url.path == "/v1/chat/completions"
        finally:
            await llm._client.close()


@pytest.mark.parametrize("path", ["free", "structured", "tools", "native"])
async def test_timeout_retry_gets_a_fresh_deadline_and_can_succeed(path):
    requests = []
    async with stub_server(_handler(requests, path, success_after=1)) as url:
        llm = _provider(url, path)
        try:
            result = await asyncio.wait_for(_invoke(llm, path, max_retries=1, initial_backoff=0.01), 2)
            assert len(requests) == 2
            assert result.content == (_Ok(ok=True) if path in ("structured", "native") else "ok")
        finally:
            await llm._client.close()


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
