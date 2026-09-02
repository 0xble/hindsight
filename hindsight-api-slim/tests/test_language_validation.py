import dataclasses
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from hindsight_api.engine.language_validation import (
    LanguageValidationOutcome,
    profile_language,
    validate_output_language,
)
from hindsight_api.engine.response_models import TokenUsage

ENGLISH = (
    "The deployment process must preserve the source language and reject generated facts "
    "that silently translate the original evidence into another language."
)
SPANISH = (
    "El proceso de despliegue debe conservar el idioma de origen y rechazar los hechos "
    "generados que traduzcan silenciosamente la evidencia original a otro idioma."
)
PORTUGUESE = (
    "O processo de implantação deve preservar o idioma de origem e rejeitar fatos gerados "
    "que traduzam silenciosamente a evidência original para outro idioma."
)
CHINESE = (
    "部署过程必须保留原始来源的语言，并拒绝把证据悄悄翻译成其他语言的生成事实，"
    "这样每一条记忆都能忠实反映输入内容并保持可追溯性。"
)
JAPANESE = "この文章は日本語で書かれており、生成された記憶も元の日本語のまま保存されなければなりません。"


def test_confident_cross_language_output_is_rejected():
    result = validate_output_language(source_text=ENGLISH, output_text=SPANISH)
    assert result.outcome is LanguageValidationOutcome.MISMATCH
    assert result.expected_languages == frozenset({"en"})
    assert result.output_language == "es"


def test_one_translated_fact_cannot_hide_inside_an_english_response():
    translated = (
        "La actualización del sistema debe conservar cada identificador original y nunca "
        "traducir los datos antes de almacenarlos en la base de conocimiento."
    )
    results = [validate_output_language(source_text=ENGLISH, output_text=output) for output in (ENGLISH, translated)]
    assert results[0].outcome is LanguageValidationOutcome.MATCH
    assert results[1].outcome is LanguageValidationOutcome.MISMATCH


def test_matching_non_english_output_is_accepted():
    result = validate_output_language(source_text=JAPANESE, output_text=JAPANESE)

    assert result.outcome is LanguageValidationOutcome.MATCH
    assert result.expected_languages == frozenset({"ja"})
    assert result.output_language == "ja"


def test_explicit_output_language_overrides_source():
    result = validate_output_language(
        source_text=SPANISH,
        output_text=ENGLISH,
        output_language="English",
    )

    assert result.outcome is LanguageValidationOutcome.MATCH
    assert result.expected_languages == frozenset({"en"})


@pytest.mark.parametrize(
    ("configured", "output", "expected"),
    [
        ("en-US", ENGLISH, "en"),
        ("pt-BR", PORTUGUESE, "pt"),
        ("Portuguese (Brazil)", PORTUGUESE, "pt"),
        ("zh-TW", CHINESE, "zh"),
    ],
)
def test_explicit_output_language_locales_resolve_to_detector_base_codes(configured, output, expected):
    result = validate_output_language(
        source_text=SPANISH,
        output_text=output,
        output_language=configured,
    )

    assert result.outcome is LanguageValidationOutcome.MATCH
    assert result.expected_languages == frozenset({expected})


def test_short_or_language_neutral_text_is_indeterminate():
    result = validate_output_language(
        source_text="Run `docker --debug` for https://example.com/a/123.",
        output_text="Ejecuta `docker --debug`.",
    )

    assert result.outcome is LanguageValidationOutcome.INDETERMINATE
    assert profile_language("A half emoji slipped into the transcript.").primary is None


def test_mixed_source_allows_each_confident_source_language():
    source = f"{ENGLISH}\n\n{SPANISH}"

    assert validate_output_language(source_text=source, output_text=ENGLISH).outcome is LanguageValidationOutcome.MATCH
    assert validate_output_language(source_text=source, output_text=SPANISH).outcome is LanguageValidationOutcome.MATCH


def test_profile_strips_code_urls_and_identifiers_before_detection():
    profile = profile_language(
        "The deployment failed after dependency resolution and should be retried with diagnostics. "
        "`Effect.tryPromise({ token: API_KEY_1234 })` https://example.com/abc 9dbd7f4a-0ad2-4eb8-9398-acde12345678"
    )

    assert profile.primary == "en"
    assert profile.analyzable_chars >= 70


def test_english_grammar_prevents_a_dutch_proper_noun_false_positive():
    text = (
        "Zoekt is a collection of Go programs: zoekt-git-index, zoekt-local-sync, and "
        "zoekt-indexserver | When: Saturday, August 08, 2026 | Involving: Hermes Agent"
    )
    assert profile_language(text).primary == "en"


def test_repetitive_role_prefixed_text_is_indeterminate():
    repeated = "\n".join(
        f"[role: user] turn {i}: alpha bravo charlie delta echo foxtrot golf hotel india juliet kilo lima mike november"
        for i in range(27)
    )

    assert profile_language(repeated).primary is None


def _retain_response(what: str) -> tuple[dict, TokenUsage]:
    return (
        {
            "facts": [
                {
                    "what": what,
                    "when": "N/A",
                    "where": "N/A",
                    "who": "N/A",
                    "why": "N/A",
                    "fact_type": "world",
                    "fact_kind": "conversation",
                }
            ]
        },
        TokenUsage(),
    )


def _retain_config(output_language: str | None = None):
    from hindsight_api.config import _get_raw_config

    return dataclasses.replace(
        _get_raw_config(),
        retain_llm_max_retries=0,
        retain_extraction_mode="concise",
        retain_extract_causal_links=False,
        retain_mission=None,
        llm_temperature_retain=0.0,
        llm_strict_schema_retain=False,
        entity_labels=None,
        entities_allow_free_form=True,
        llm_output_language=output_language,
    )


@pytest.mark.asyncio
async def test_retain_retries_mismatch_once_and_never_returns_rejected_fact():
    from hindsight_api.engine.llm_wrapper import LLMProvider
    from hindsight_api.engine.retain.fact_extraction import _extract_facts_from_chunk

    llm = MagicMock(spec=LLMProvider)
    llm.provider = "test-language-drift"
    llm.model = "mock-language-drift"
    llm.call = AsyncMock(side_effect=[_retain_response(SPANISH), _retain_response(ENGLISH)])

    with patch(
        "hindsight_api.engine.retain.fact_extraction._build_extraction_prompt_and_schema",
        return_value=("system prompt", MagicMock()),
    ):
        facts, _ = await _extract_facts_from_chunk(
            chunk=ENGLISH,
            chunk_index=0,
            total_chunks=1,
            event_date=datetime(2026, 9, 1, tzinfo=timezone.utc),
            context="",
            llm_config=llm,
            config=_retain_config(),
        )

    assert [getattr(fact, "fact") for fact in facts] == [ENGLISH]
    assert llm.call.await_count == 2
    retry_message = llm.call.await_args_list[1].kwargs["messages"][-1]["content"]
    assert "previous response was rejected" in retry_message.lower()


@pytest.mark.asyncio
async def test_retain_retry_honors_configured_output_language():
    from hindsight_api.engine.llm_wrapper import LLMProvider
    from hindsight_api.engine.retain.fact_extraction import _extract_facts_from_chunk

    llm = MagicMock(spec=LLMProvider)
    llm.provider = "test-language-drift"
    llm.model = "mock-language-drift"
    llm.call = AsyncMock(side_effect=[_retain_response(SPANISH), _retain_response(ENGLISH)])

    with patch(
        "hindsight_api.engine.retain.fact_extraction._build_extraction_prompt_and_schema",
        return_value=("system prompt", MagicMock()),
    ):
        facts, _ = await _extract_facts_from_chunk(
            chunk=SPANISH,
            chunk_index=0,
            total_chunks=1,
            event_date=datetime(2026, 9, 1, tzinfo=timezone.utc),
            context="",
            llm_config=llm,
            config=_retain_config("English"),
        )

    assert [getattr(fact, "fact") for fact in facts] == [ENGLISH]
    retry_message = llm.call.await_args_list[1].kwargs["messages"][-1]["content"]
    assert "configured output language: English" in retry_message
    assert "Do not translate the source" not in retry_message


@pytest.mark.asyncio
async def test_retain_fails_closed_after_second_confident_mismatch():
    from hindsight_api.engine.llm_wrapper import LLMProvider
    from hindsight_api.engine.retain.fact_extraction import _extract_facts_from_chunk

    llm = MagicMock(spec=LLMProvider)
    llm.provider = "test-language-drift"
    llm.model = "mock-language-drift"
    llm.call = AsyncMock(side_effect=[_retain_response(SPANISH), _retain_response(SPANISH)])

    with (
        patch(
            "hindsight_api.engine.retain.fact_extraction._build_extraction_prompt_and_schema",
            return_value=("system prompt", MagicMock()),
        ),
        pytest.raises(RuntimeError, match="generated_language_mismatch"),
    ):
        await _extract_facts_from_chunk(
            chunk=ENGLISH,
            chunk_index=0,
            total_chunks=1,
            event_date=datetime(2026, 9, 1, tzinfo=timezone.utc),
            context="",
            llm_config=llm,
            config=_retain_config(),
        )

    assert llm.call.await_count == 2


def _consolidation_config(output_language: str | None = None):
    config = MagicMock()
    config.observations_mission = None
    config.consolidation_max_attempts = 1
    config.consolidation_llm_max_retries = None
    config.consolidation_max_completion_tokens = None
    config.llm_strict_schema_consolidation = False
    config.llm_temperature_consolidation = 0.0
    config.llm_supports_max_items = True
    config.llm_output_language = output_language
    return config


@pytest.mark.asyncio
async def test_consolidation_retries_mismatch_atomically():
    from hindsight_api.engine.consolidation.consolidator import (
        _consolidate_batch_with_llm,
        _ConsolidationBatchResponse,
        _CreateAction,
    )

    llm = AsyncMock()
    llm._provider_impl = None
    llm.provider = "test-language-drift"
    llm.call.side_effect = [
        _ConsolidationBatchResponse(creates=[_CreateAction(text=SPANISH, source_fact_ids=["m1"])]),
        _ConsolidationBatchResponse(creates=[_CreateAction(text=ENGLISH, source_fact_ids=["m1"])]),
    ]

    result = await _consolidate_batch_with_llm(
        llm_config=llm,
        memories=[{"id": "m1", "text": ENGLISH}],
        union_observations=[],
        union_source_facts={},
        config=_consolidation_config(),
    )

    assert not result.failed
    assert [create.text for create in result.creates] == [ENGLISH]
    assert llm.call.await_count == 2
    retry_message = llm.call.await_args_list[1].kwargs["messages"][-1]["content"]
    assert "previous response was rejected" in retry_message.lower()


@pytest.mark.asyncio
async def test_consolidation_retry_honors_configured_output_language():
    from hindsight_api.engine.consolidation.consolidator import (
        _consolidate_batch_with_llm,
        _ConsolidationBatchResponse,
        _CreateAction,
    )

    llm = AsyncMock()
    llm._provider_impl = None
    llm.provider = "test-language-drift"
    llm.call.side_effect = [
        _ConsolidationBatchResponse(creates=[_CreateAction(text=SPANISH, source_fact_ids=["m1"])]),
        _ConsolidationBatchResponse(creates=[_CreateAction(text=ENGLISH, source_fact_ids=["m1"])]),
    ]

    result = await _consolidate_batch_with_llm(
        llm_config=llm,
        memories=[{"id": "m1", "text": SPANISH}],
        union_observations=[],
        union_source_facts={},
        config=_consolidation_config("English"),
    )

    assert not result.failed
    assert [create.text for create in result.creates] == [ENGLISH]
    retry_message = llm.call.await_args_list[1].kwargs["messages"][-1]["content"]
    assert "configured output language: English" in retry_message
    assert "Do not translate the source" not in retry_message


@pytest.mark.asyncio
async def test_consolidation_fails_closed_after_second_mismatch():
    from hindsight_api.engine.consolidation.consolidator import (
        _consolidate_batch_with_llm,
        _ConsolidationBatchResponse,
        _CreateAction,
    )

    llm = AsyncMock()
    llm._provider_impl = None
    llm.provider = "test-language-drift"
    rejected = _ConsolidationBatchResponse(creates=[_CreateAction(text=SPANISH, source_fact_ids=["m1"])])
    llm.call.side_effect = [rejected, rejected]

    result = await _consolidate_batch_with_llm(
        llm_config=llm,
        memories=[{"id": "m1", "text": ENGLISH}],
        union_observations=[],
        union_source_facts={},
        config=_consolidation_config(),
    )

    assert result.failed
    assert not result.creates
    assert not result.updates
    assert llm.call.await_count == 2


def test_terminal_language_mismatch_is_non_retryable_for_worker():
    from hindsight_api.engine.language_validation import GeneratedLanguageMismatch
    from hindsight_api.engine.memory_engine import _is_non_retryable_task_error

    assert _is_non_retryable_task_error(GeneratedLanguageMismatch("still translated"))
