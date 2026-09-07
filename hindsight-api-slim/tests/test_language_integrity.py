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
    evaluate_language_integrity,
    find_mismatches,
    find_mismatches_safely,
    has_introduced_script_prose,
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
TYPESCRIPT_SOURCE = (
    "TypeScript operating rules require strict mode, a pinned compiler version, shared types when useful, "
    "runtime validation distinct from compile-time types, generated clients bound to committed source schemas, "
    "and domain rules outside React components, Server Actions, route handlers, and provider functions."
)
MIXED_LANGUAGE_TYPESCRIPT_DRIFT = (
    "TypeScript stack 的 operating rules 要求启用 strict mode、pin compiler version、在有用时共享 types、"
    "不要把 compile-time types 误认为 runtime validation、让 generated clients 绑定到 committed source schemas，"
    "并且不要把 domain rules 藏在 React components、Server Actions、route handlers 或 provider functions 中。"
)


@pytest.mark.asyncio
async def test_detects_historical_same_script_translation() -> None:
    context = await prepare_context({"source": ENGLISH_SOURCE})

    mismatches = await find_mismatches(context, [GeneratedText("fact:0", SPANISH_DRIFT, ("source",))])

    assert [(item.source_language, item.generated_language) for item in mismatches] == [("en", "es")]


@pytest.mark.asyncio
async def test_detects_introduced_han_prose_when_language_id_abstains() -> None:
    context = await prepare_context({"source": TYPESCRIPT_SOURCE})

    result = await evaluate_language_integrity(
        context,
        [GeneratedText("fact:0", MIXED_LANGUAGE_TYPESCRIPT_DRIFT, ("source",))],
    )

    assert result.checked == 1
    assert [(item.source_language, item.generated_language) for item in result.mismatches] == [("en", "wuu")]


@pytest.mark.asyncio
async def test_detects_novel_script_prose_when_source_profile_abstains() -> None:
    context = await prepare_context({"source": "Use strict mode."})

    result = await evaluate_language_integrity(
        context,
        [GeneratedText("fact:0", "Use strict mode。请保持测试覆盖并启用安全校验。", ("source",))],
    )

    assert context.source_profiles["source"].actionable is False
    assert result.checked == 1
    assert len(result.mismatches) == 1


@pytest.mark.parametrize(
    ("source", "generated"),
    [
        (
            TYPESCRIPT_SOURCE + ' The customer said, "这是保留的中文引文，不应被当作新的翻译内容。"',
            'The customer said, "这是保留的中文引文，不应被当作新的翻译内容。"',
        ),
        (TYPESCRIPT_SOURCE, "Keep this literal code: `const 标签 = '这是示例代码中的文本内容';`"),
        (TYPESCRIPT_SOURCE, "The attendee was 王小明."),
    ],
)
def test_source_relative_script_check_exempts_quotes_code_and_names(source: str, generated: str) -> None:
    assert has_introduced_script_prose(source, generated) is False


@pytest.mark.parametrize("source", ["", "`const 标签 = '文本';`", "这是中文源文本。", "ქართული წყარო"])
def test_source_relative_script_check_requires_latin_source_evidence(source: str) -> None:
    assert has_introduced_script_prose(source, "Vitest 是 TypeScript 的首选 unit test runner。") is False


def test_source_relative_script_check_rejects_novel_prose_despite_a_source_quote() -> None:
    source = TYPESCRIPT_SOURCE + ' The customer said, "这是保留的中文引文，不应被当作新的翻译内容。"'
    generated = """The customer said, "请使用不同的中文句子来测试新出现的翻译内容是否会被正确识别。"""

    assert has_introduced_script_prose(source, generated) is True


def test_source_relative_script_check_preserves_partial_copied_quote() -> None:
    source = TYPESCRIPT_SOURCE + ' The customer said, "这是保留的中文引文，不应被当作新的翻译内容。"'

    assert has_introduced_script_prose(source, "当作新的翻译") is False


@pytest.mark.parametrize(
    "generated",
    [
        "fixtures 应保持 explicit 和 safe。",
        "Vitest 是 TypeScript 的首选 unit 和 integration test runner。",
        "测试边界应优先选择 highest stable existing boundary，只要它仍然 deterministic 且 diagnosable。",
    ],
)
def test_source_relative_script_check_catches_short_novel_prose(generated: str) -> None:
    assert has_introduced_script_prose(TYPESCRIPT_SOURCE, generated) is True


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


def test_language_integrity_defaults_for_legacy_constructor_input() -> None:
    legacy_values = vars(HindsightConfig.from_env()).copy()
    legacy_values.pop("llm_language_integrity")

    assert HindsightConfig(**legacy_values).llm_language_integrity == "observe"


@pytest.mark.asyncio
async def test_evaluation_distinguishes_abstention_from_pass() -> None:
    context = await prepare_context({"source": "short text"})

    result = await evaluate_language_integrity(
        context,
        [GeneratedText("fact:0", "texto corto", ("source",))],
    )

    assert result.mismatches == ()
    assert result.checked == 0
    assert result.abstained == 1


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
        patch.object(guard, "_evaluate_sync", side_effect=RuntimeError("detector unavailable")),
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
