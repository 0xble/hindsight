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
