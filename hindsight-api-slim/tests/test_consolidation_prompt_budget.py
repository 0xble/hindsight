"""Regression coverage for local consolidation prompt budgeting."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from hindsight_api.engine.consolidation import consolidator


@pytest.mark.parametrize(
    "message",
    [
        "context_length_exceeded",
        "maximum context length is 128000 tokens",
        "input token limit exceeded",
    ],
)
def test_context_limit_errors_fail_fast(message: str):
    """An unchanged oversized prompt must go straight to adaptive splitting."""
    failure_class = consolidator._classify_batch_failure(RuntimeError(message))

    assert failure_class is consolidator._BatchFailureClass.FAIL_FAST


@pytest.mark.asyncio
async def test_oversized_consolidation_prompt_is_split_before_llm_call(monkeypatch):
    """A local budget failure must avoid retrying an identical provider request."""
    monkeypatch.setattr(consolidator, "build_consolidation_system_prompt", lambda **_: "system")
    monkeypatch.setattr(consolidator, "build_consolidation_input", lambda **_: "x" * 1000)
    llm = SimpleNamespace(call=AsyncMock())
    config = SimpleNamespace(
        consolidation_max_context_tokens=10,
        llm_output_language=None,
        observations_mission=None,
        llm_supports_max_items=False,
        consolidation_max_attempts=3,
        consolidation_llm_max_retries=3,
        llm_strict_schema_consolidation=False,
        consolidation_max_completion_tokens=None,
    )

    result = await consolidator._consolidate_batch_with_llm(
        llm_config=llm,
        memories=[{"id": "memory-1", "text": "fact"}],
        union_observations=[],
        union_source_facts={},
        config=config,
    )

    assert result.failed is True
    llm.call.assert_not_awaited()
