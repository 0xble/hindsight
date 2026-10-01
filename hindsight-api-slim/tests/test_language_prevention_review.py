"""Deterministic review regressions: real detector, no provider generation."""

import json

import pytest

from hindsight_api.engine import language_integrity as guard
from tests.test_language_prevention import ENGLISH, FRENCH, SPANISH


def check(source, output):
    context = guard._prepare_context_sync({"s": source})
    return guard._evaluate_sync(context, [guard.GeneratedText("f", output, ("s",))])


def test_source_profiling_is_shared_by_chunk_not_repeated_per_fact(monkeypatch):
    profiles = []
    original = guard._profile

    def counted(text, *, source):
        if source:
            profiles.append(text)
        return original(text, source=source)

    monkeypatch.setattr(guard, "_profile", counted)
    context = guard._prepare_context_sync({str(i): ENGLISH for i in range(20)})
    guard._evaluate_sync(
        context,
        [
            guard.GeneratedText(
                str(i), "The team has preserved the records and completed the review successfully.", (str(i),)
            )
            for i in range(20)
        ],
    )
    assert profiles == [ENGLISH]


def test_routine_english_outputs_do_not_rescan_source_scripts(monkeypatch):
    source_scans = []
    original = guard._script_counts

    def counted(text):
        if text == ENGLISH:
            source_scans.append(text)
        return original(text)

    monkeypatch.setattr(guard, "_script_counts", counted)
    context = guard._prepare_context_sync({"s": ENGLISH})
    preparation_scans = len(source_scans)
    guard._evaluate_sync(
        context,
        [
            guard.GeneratedText(
                str(i), "The team has preserved the records and completed the review successfully.", ("s",)
            )
            for i in range(20)
        ],
    )
    assert len(source_scans) == preparation_scans


RUSSIAN_SOURCE = "Команда завершила проверку и сохранила исходные документы для следующего выпуска."
RUSSIAN_PARAPHRASE = "Исходные документы сохранены после проверки, и команда подготовилась к следующему выпуску."


@pytest.mark.parametrize("separate_sources", [False, True])
def test_minority_source_language_supports_cross_script_paraphrase(separate_sources: bool) -> None:
    english = (ENGLISH + "\n") * 10
    sources = {"en": english, "ru": RUSSIAN_SOURCE} if separate_sources else {"s": english + RUSSIAN_SOURCE}
    context = guard._prepare_context_sync(sources)
    keys = tuple(sources)

    # Cyrillic evidence is substantive but below the document-level mixed share.
    assert guard._letter_count(RUSSIAN_SOURCE) / guard._letter_count(english + RUSSIAN_SOURCE) < 0.20
    assert context.source_profiles[keys[0]].language == "en"
    assert context.source_profiles[keys[0]].actionable
    result = guard._evaluate_sync(context, [guard.GeneratedText("f", RUSSIAN_PARAPHRASE, keys)])

    assert result.verdicts[0].status == "preserved"
    assert not guard.enforcement_failures(result, guard.LanguageIntegrityMode.REJECT)
    assert set().union(*(context.supported_languages[key] for key in keys)) == {"en", "ru"}


@pytest.mark.parametrize("wrapper", ["{}", '"{}"', "`{}`"])
def test_only_unquoted_source_prose_authorizes_cross_script_paraphrase(wrapper: str) -> None:
    source = (ENGLISH + "\n") * 10 + wrapper.format(RUSSIAN_SOURCE)
    result = check(source, RUSSIAN_PARAPHRASE)

    if wrapper == "{}":
        assert not guard.enforcement_failures(result, guard.LanguageIntegrityMode.REJECT)
    else:
        assert result.verdicts[0].status == "mismatch"


def test_supported_script_does_not_authorize_unsupported_language() -> None:
    source = (ENGLISH + "\n") * 10 + RUSSIAN_SOURCE
    ukrainian = "Команда завершила перевірку та зберегла початкові документи для наступного випуску."
    result = check(source, ukrainian)

    assert result.verdicts[0].status == "mismatch"
    assert result.mismatches[0].generated_language == "uk"


def test_uncited_source_language_does_not_authorize_paraphrase() -> None:
    context = guard._prepare_context_sync({"en": ENGLISH, "ru": RUSSIAN_SOURCE})
    result = guard._evaluate_sync(context, [guard.GeneratedText("f", RUSSIAN_PARAPHRASE, ("en",))])

    assert result.verdicts[0].status == "mismatch"


def test_minority_source_script_does_not_authorize_new_third_script() -> None:
    source = (ENGLISH + "\n") * 10 + RUSSIAN_SOURCE
    output = RUSSIAN_PARAPHRASE.rstrip(".") + " 请保留原始记录并完成安全检查。"

    assert check(source, output).verdicts[0].status == "mismatch"


def test_capitalization_is_not_name_evidence():
    result = check(ENGLISH, "Alles Gut")
    assert result.verdicts[0].status == "unchecked"
    assert guard.enforcement_failures(result, guard.LanguageIntegrityMode.REJECT)


@pytest.mark.parametrize(
    "quote",
    [
        "'{}'",
        '"{}"',
        "“{}”",
        "‘{}’",
        "«{}»",
        '"\n{}\n"',
        "\n> {}\n",
    ],
)
@pytest.mark.parametrize("transcript", [False, True])
def test_quoted_foreign_prose_only_authorizes_copying(quote, transcript):
    source = ENGLISH + "\nThe report quoted:\n" + quote.format(FRENCH * 4)
    if transcript:
        source = json.dumps({"messages": [{"role": "user", "content": source}]})
    result = check(
        source, "Le rapport présente les conclusions de la réunion et les recommandations pour la prochaine étape."
    )
    assert guard.enforcement_failures(result, guard.LanguageIntegrityMode.REJECT)
    assert not guard.enforcement_failures(check(source, FRENCH), guard.LanguageIntegrityMode.REJECT)


@pytest.mark.parametrize(
    "source",
    [
        json.dumps({"text": ENGLISH}),
        json.dumps({"messages": [{"role": "user", "content": ENGLISH}]}),
        json.dumps({"role": "user", "content": ENGLISH}) + "\n" + json.dumps({"role": "assistant", "content": ENGLISH}),
    ],
)
def test_structured_source_values_are_prose_not_quotes(source):
    output = "The team finished the review and will retain the original records for the next release."
    assert not guard.enforcement_failures(check(source, output), guard.LanguageIntegrityMode.REJECT)


@pytest.mark.parametrize(
    "output",
    [
        "The team completed the review, pero el equipo necesita revisar los datos para la próxima reunión, and will preserve the original records.",
        "The team completed the review and the tests " + SPANISH.rstrip(".") + " before the release.",
        (ENGLISH.replace(".", "") + " ") * 5
        + "y el equipo necesita revisar los datos para la próxima reunión "
        + ENGLISH.replace(".", "") * 5,
        "`" + SPANISH + "`",
        "```text\n" + SPANISH + "\n```",
    ],
)
def test_inline_foreign_prose_cannot_hide_in_sentence_or_backticks(output):
    assert guard.enforcement_failures(check(ENGLISH, output), guard.LanguageIntegrityMode.REJECT)


@pytest.mark.parametrize("output", ["Alice Smith", "`const 标签 = '这是示例代码中的文本内容';`", "`print('hello')`"])
def test_source_names_and_real_code_are_preserved(output):
    assert not guard.enforcement_failures(
        check(ENGLISH + " Alice Smith led the review.", output), guard.LanguageIntegrityMode.REJECT
    )


@pytest.mark.asyncio
async def test_retain_validates_dimensions_without_generated_labels():
    from hindsight_api.engine.response_models import LLMCallResult, TokenUsage
    from tests.test_language_integrity_retain import ENGLISH_FACT, ENGLISH_SOURCE, _extract, _llm

    llm = _llm()
    llm.call.side_effect = [
        LLMCallResult(
            content={
                "facts": [{"what": ENGLISH_FACT, "when": "2026-09-04", "who": "Alice Smith", "fact_type": "world"}]
            },
            usage=TokenUsage(),
        )
    ] * 2
    facts, _ = await _extract("reject", llm, source=ENGLISH_SOURCE + " Alice Smith led the team on 2026-09-04.")
    assert llm.call.await_count == 1
    assert "When: 2026-09-04 | Involving: Alice Smith" in facts[0].fact


@pytest.mark.asyncio
async def test_retain_does_not_ignore_foreign_dimension():
    from hindsight_api.engine.response_models import LLMCallResult, TokenUsage
    from tests.test_language_integrity_retain import ENGLISH_FACT, _extract, _llm

    llm = _llm()
    llm.call.side_effect = [
        LLMCallResult(
            content={"facts": [{"what": ENGLISH_FACT, "why": SPANISH, "fact_type": "world"}]}, usage=TokenUsage()
        )
    ] * 2
    with pytest.raises(guard.GeneratedLanguageMismatch):
        await _extract("reject", llm)
    assert llm.call.await_count == 2
