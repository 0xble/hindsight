import dataclasses
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from hindsight_api.config import _get_raw_config
from hindsight_api.engine.language_integrity import GeneratedLanguageMismatch
from hindsight_api.engine.llm_wrapper import LLMProvider
from hindsight_api.engine.response_models import LLMCallResult, TokenUsage
from hindsight_api.engine.retain.fact_extraction import _extract_facts_from_chunk

ENGLISH_SOURCE = (
    "The operations team completed the important review findings and the low-cost hardening work "
    "through regression tests, then ran the focused and canonical validation suites successfully."
)
SPANISH_DRIFT = (
    "El equipo de operaciones completó los hallazgos importantes de la revisión y el trabajo de "
    "endurecimiento mediante pruebas de regresión, y luego ejecutó correctamente las validaciones canónicas."
)
ENGLISH_FACT = (
    "The operations team completed the review and hardening work through regression tests and canonical validation."
)


def _config(mode: str):
    return dataclasses.replace(
        _get_raw_config(),
        llm_language_integrity=mode,
        llm_output_language=None,
        retain_llm_max_retries=0,
        llm_max_retries=0,
        retain_llm_initial_backoff=None,
        llm_initial_backoff=0.0,
        retain_llm_max_backoff=None,
        llm_max_backoff=0.0,
        retain_max_completion_tokens=8192,
        retain_extraction_mode="concise",
        retain_extract_causal_links=False,
        retain_mission=None,
        llm_temperature_retain=0.1,
        llm_strict_schema_retain=False,
    )


def _response(text: str) -> LLMCallResult:
    return LLMCallResult(
        content={"facts": [{"what": text, "fact_type": "world", "fact_kind": "conversation"}]},
        usage=TokenUsage(),
    )


def _llm(*texts: str):
    llm = MagicMock(spec=LLMProvider)
    llm.provider = "mock"
    llm.model = "mock-model"
    llm.call = AsyncMock(side_effect=[_response(text) for text in texts])
    return llm


async def _extract(mode: str, llm, *, content_retries: int = 0):
    with patch(
        "hindsight_api.engine.retain.fact_extraction._build_extraction_prompt_and_schema",
        return_value=("system prompt", MagicMock()),
    ):
        return await _extract_facts_from_chunk(
            chunk=ENGLISH_SOURCE,
            chunk_index=0,
            total_chunks=1,
            event_date=datetime(2026, 9, 4, tzinfo=timezone.utc),
            context="",
            llm_config=llm,
            config=dataclasses.replace(
                _config(mode),
                retain_llm_max_retries=content_retries,
                llm_max_retries=content_retries,
            ),
            agent_name="test-agent",
        )


@pytest.mark.asyncio
async def test_retry_mode_regenerates_once_and_returns_corrected_facts() -> None:
    llm = _llm(SPANISH_DRIFT, ENGLISH_FACT)

    facts, _ = await _extract("retry", llm)

    assert llm.call.await_count == 2
    assert facts[0].fact.startswith("The operations team")
    second_user_message = llm.call.await_args_list[1].kwargs["messages"][1]["content"]
    assert "LANGUAGE CORRECTION" in second_user_message
    assert "source's language" in second_user_message
    assert "ISO 639" not in second_user_message


@pytest.mark.asyncio
async def test_retry_mode_is_non_destructive_after_second_mismatch() -> None:
    llm = _llm(SPANISH_DRIFT, SPANISH_DRIFT)

    facts, _ = await _extract("retry", llm)

    assert llm.call.await_count == 2
    assert facts[0].fact.startswith("El equipo")


@pytest.mark.asyncio
async def test_reject_mode_fails_only_after_corrective_retry() -> None:
    llm = _llm(SPANISH_DRIFT, SPANISH_DRIFT)

    with pytest.raises(GeneratedLanguageMismatch):
        await _extract("reject", llm)

    assert llm.call.await_count == 2


@pytest.mark.asyncio
async def test_observe_mode_neither_retries_nor_rejects() -> None:
    llm = _llm(SPANISH_DRIFT)

    facts, _ = await _extract("observe", llm)

    assert llm.call.await_count == 1
    assert facts[0].fact.startswith("El equipo")
    first_user_message = llm.call.await_args.kwargs["messages"][1]["content"]
    assert "LANGUAGE INTEGRITY" not in first_user_message
    assert "LANGUAGE CORRECTION" not in first_user_message


@pytest.mark.asyncio
async def test_language_retry_does_not_consume_content_retry_budget() -> None:
    llm = _llm(SPANISH_DRIFT, ENGLISH_FACT)
    llm.call.side_effect = [
        LLMCallResult(content="not json", usage=TokenUsage()),
        _response(SPANISH_DRIFT),
        _response(ENGLISH_FACT),
    ]

    facts, _ = await _extract("retry", llm, content_retries=1)

    assert llm.call.await_count == 3
    assert facts[0].fact.startswith("The operations team")
