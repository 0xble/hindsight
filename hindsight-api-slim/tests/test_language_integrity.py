import os
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from hindsight_api.config import HindsightConfig
from hindsight_api.engine import language_integrity as guard
from hindsight_api.engine.language_integrity import (
    GeneratedLanguageMismatch,
    GeneratedText,
    LanguageIntegrityMode,
    LanguageIntegrityUnavailable,
    build_retry_instruction,
    build_source_instruction,
    configured_mode,
    find_mismatches,
    find_mismatches_safely,
    prepare_context,
    prepare_context_safely,
    record_outcome,
    should_check,
)

ENGLISH_SOURCE = (
    "The resolution of the important review findings and the low-cost hardening suggestions "
    "was completed through regression tests, followed by the focused and canonical validations."
)
SPANISH_DRIFT = (
    "Se completó la resolución de los hallazgos importantes de revisión y de las sugerencias "
    "de endurecimiento mediante pruebas de regresión, seguidas de las validaciones canónicas."
)


@pytest.mark.asyncio
async def test_detects_historical_same_script_translation() -> None:
    context = await prepare_context({"source": ENGLISH_SOURCE})

    mismatches = await find_mismatches(context, [GeneratedText("fact:0", SPANISH_DRIFT, ("source",))])

    assert [(item.source_language, item.generated_language) for item in mismatches] == [("en", "es")]


@pytest.mark.asyncio
async def test_preserves_matching_non_english_output() -> None:
    context = await prepare_context({"source": SPANISH_DRIFT})

    assert await find_mismatches(context, [GeneratedText("fact:0", SPANISH_DRIFT, ("source",))]) == ()


@pytest.mark.asyncio
async def test_normal_japanese_is_not_treated_as_mixed_and_chinese_drift_is_detected() -> None:
    japanese = (
        "運用チームは重要なレビュー結果を確認し、低コストの改善を完了しました。"
        "担当者は次回の会議で変更内容を説明し、全員が同じ手順を使うことを確認します。"
    )
    chinese = "运营团队审查了重要结果并完成了低成本改进，负责人将在下次会议上说明这些变更。"
    context = await prepare_context({"source": japanese})

    assert context.source_profiles["source"].mixed is False
    assert await find_mismatches(context, [GeneratedText("fact:0", chinese, ("source",))])


@pytest.mark.asyncio
async def test_preserved_foreign_language_quote_is_not_treated_as_drift() -> None:
    quote = "顧客は商品が届かず、返金を希望していると明確に説明しました。"
    source = (
        "The support agent recorded the complaint exactly as written so the operations team could review it. "
        f"The customer's original statement was: '{quote}' The agent did not translate the quotation."
    )
    context = await prepare_context({"source": source})

    assert await find_mismatches(context, [GeneratedText("fact:0", quote, ("source",))]) == ()


@pytest.mark.asyncio
async def test_unknown_consolidation_source_key_abstains() -> None:
    context = await prepare_context({"known": ENGLISH_SOURCE})

    assert (
        await find_mismatches(
            context,
            [GeneratedText("observation:0", SPANISH_DRIFT, ("known", "missing"))],
        )
        == ()
    )


@pytest.mark.asyncio
async def test_abstains_for_materially_multilingual_source() -> None:
    source = (
        ENGLISH_SOURCE
        + "\n\n"
        + "El segundo párrafo está escrito deliberadamente en español y contiene suficiente texto para ser material. "
        + "También explica que una entrada multilingüe legítima no debe bloquearse."
    )
    context = await prepare_context({"source": source})

    assert context.source_profiles["source"].mixed is True
    assert await find_mismatches(context, [GeneratedText("fact:0", SPANISH_DRIFT, ("source",))]) == ()


@pytest.mark.asyncio
async def test_abstains_when_consolidation_sources_disagree() -> None:
    context = await prepare_context({"en": ENGLISH_SOURCE, "es": SPANISH_DRIFT})

    assert await find_mismatches(context, [GeneratedText("create:0", SPANISH_DRIFT, ("en", "es"))]) == ()


@pytest.mark.asyncio
async def test_technical_english_false_positive_is_vetoed_by_lexical_confirmation() -> None:
    source = (
        "Ellie Lambert is a licensed clinical social worker and community organizer. "
        "She works with local partners and writes detailed program reports for the team."
    )
    output = (
        "Ellie Lambert is a licensed clinical social worker who has spent nearly 20 years supporting girls, "
        "young women, and families through organizations such as Freely in Hope, where she helps participants "
        "develop leadership skills and build sustainable community programs."
    )
    context = await prepare_context({"source": source})

    assert await find_mismatches(context, [GeneratedText("fact:0", output, ("source",))]) == ()


@pytest.mark.asyncio
async def test_profiles_sources_once_across_corrective_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    source_profiles = 0
    original = guard._profile

    def counted(text: str, *, source: bool):
        nonlocal source_profiles
        source_profiles += int(source)
        return original(text, source=source)

    monkeypatch.setattr(guard, "_profile", counted)
    context = await prepare_context({"source": ENGLISH_SOURCE})
    generated = [GeneratedText("fact:0", SPANISH_DRIFT, ("source",))]

    assert await find_mismatches(context, generated)
    assert await find_mismatches(context, generated)
    assert source_profiles == 1


def test_mode_is_configurable_and_explicit_output_language_disables_source_guard() -> None:
    retry = SimpleNamespace(llm_language_integrity="retry", llm_output_language=None)
    explicit = SimpleNamespace(llm_language_integrity="reject", llm_output_language="English")

    assert configured_mode(retry) is LanguageIntegrityMode.RETRY
    assert should_check(retry) is True
    assert should_check(explicit) is False


@pytest.mark.asyncio
async def test_instructions_avoid_source_text_and_detector_codes() -> None:
    context = await prepare_context({"source": ENGLISH_SOURCE})
    mismatch = await find_mismatches(context, [GeneratedText("fact:0", SPANISH_DRIFT, ("source",))])

    source_instruction = build_source_instruction(context, ("source",))
    retry_instruction = build_retry_instruction(mismatch)
    error = GeneratedLanguageMismatch(mismatch)

    assert "Preserve the source's language" in source_instruction
    assert "'en'" not in source_instruction
    assert "source's language" in retry_instruction
    assert "ISO 639" not in retry_instruction
    assert ENGLISH_SOURCE not in source_instruction + retry_instruction + str(error)


def test_config_environment_parses_language_integrity_mode() -> None:
    with patch.dict(os.environ, {"HINDSIGHT_API_LLM_LANGUAGE_INTEGRITY": "observe"}):
        assert HindsightConfig.from_env().llm_language_integrity == "observe"


def test_config_defaults_to_observe_only() -> None:
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("HINDSIGHT_API_LLM_LANGUAGE_INTEGRITY", None)
        assert HindsightConfig.from_env().llm_language_integrity == "observe"


def test_config_environment_rejects_unknown_language_integrity_mode() -> None:
    with (
        patch.dict(os.environ, {"HINDSIGHT_API_LLM_LANGUAGE_INTEGRITY": "guess"}),
        pytest.raises(ValueError, match="HINDSIGHT_API_LLM_LANGUAGE_INTEGRITY"),
    ):
        HindsightConfig.from_env()


def test_metric_records_only_bounded_labels() -> None:
    collector = MagicMock()
    with patch("hindsight_api.metrics.get_metrics_collector", return_value=collector):
        record_outcome(stage="retain", mode=LanguageIntegrityMode.RETRY, outcome="retry")

    collector.record_language_integrity.assert_called_once_with(stage="retain", mode="retry", outcome="retry")


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", [LanguageIntegrityMode.OBSERVE, LanguageIntegrityMode.RETRY])
async def test_source_detector_failures_fail_open_outside_strict_mode(mode: LanguageIntegrityMode) -> None:
    with (
        patch.object(guard, "_prepare_context_sync", side_effect=RuntimeError("detector unavailable")),
        patch.object(guard, "record_outcome") as metric,
    ):
        assert await prepare_context_safely({"source": ENGLISH_SOURCE}, stage="retain", mode=mode) is None

    metric.assert_called_once_with(stage="retain", mode=mode, outcome="error")


@pytest.mark.asyncio
async def test_source_detector_failure_is_terminal_in_strict_mode() -> None:
    with (
        patch.object(guard, "_prepare_context_sync", side_effect=RuntimeError("detector unavailable")),
        patch.object(guard, "record_outcome") as metric,
        pytest.raises(LanguageIntegrityUnavailable),
    ):
        await prepare_context_safely(
            {"source": ENGLISH_SOURCE},
            stage="consolidation",
            mode=LanguageIntegrityMode.REJECT,
        )

    metric.assert_called_once_with(stage="consolidation", mode=LanguageIntegrityMode.REJECT, outcome="error")


@pytest.mark.asyncio
async def test_output_detector_failure_fails_open_in_retry_mode() -> None:
    context = await prepare_context({"source": ENGLISH_SOURCE})
    with (
        patch.object(guard, "_find_mismatches_sync", side_effect=RuntimeError("detector unavailable")),
        patch.object(guard, "record_outcome") as metric,
    ):
        assert (
            await find_mismatches_safely(
                context,
                [GeneratedText("fact", SPANISH_DRIFT, ("source",))],
                stage="retain",
                mode=LanguageIntegrityMode.RETRY,
            )
            == ()
        )

    metric.assert_called_once_with(stage="retain", mode=LanguageIntegrityMode.RETRY, outcome="error")
