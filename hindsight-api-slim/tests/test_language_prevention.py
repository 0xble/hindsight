"""Offline source-relative prevention regressions; no provider calls."""

import pytest

from hindsight_api.engine import language_integrity as guard

ENGLISH = "The operations team completed the review and the tests. The next release will preserve the source records and report failures for review."
FRENCH = "Le rapport décrit les résultats de la vérification et les changements nécessaires pour préserver les données."
SPANISH = "El equipo completó la revisión de los resultados y las pruebas para preservar los datos originales."


@pytest.mark.asyncio
async def test_copied_french_span_is_not_translation():
    context = await guard.prepare_context({"s": ENGLISH * 5 + '\nAudit quote: "' + FRENCH + '"'})
    result = await guard.evaluate_language_integrity(context, [guard.GeneratedText("f", FRENCH, ("s",))])
    assert not result.mismatches
    assert result.verdicts[0].status == "copied"


@pytest.mark.asyncio
async def test_latin_drift_cannot_hide_inside_english_aggregate():
    context = await guard.prepare_context({"s": ENGLISH})
    result = await guard.evaluate_language_integrity(
        context, [guard.GeneratedText("f", ENGLISH * 5 + " " + SPANISH, ("s",))]
    )
    assert result.mismatches


@pytest.mark.asyncio
async def test_every_output_has_verdict_and_unknown_never_counts_as_pass():
    context = await guard.prepare_context({"s": ENGLISH})
    result = await guard.evaluate_language_integrity(
        context, [guard.GeneratedText("good", ENGLISH, ("s",)), guard.GeneratedText("unknown", SPANISH, ("missing",))]
    )
    assert [(v.key, v.status) for v in result.verdicts] == [("good", "copied"), ("unknown", "unchecked")]
    assert result.abstained == 1
    assert guard.enforcement_failures(result, guard.LanguageIntegrityMode.REJECT)[0].key == "unknown"


@pytest.mark.asyncio
async def test_mixed_source_can_preserve_evidenced_language():
    context = await guard.prepare_context({"s": ENGLISH + "\n" + SPANISH})
    result = await guard.evaluate_language_integrity(context, [guard.GeneratedText("f", SPANISH, ("s",))])
    assert not guard.enforcement_failures(result, guard.LanguageIntegrityMode.REJECT)


@pytest.mark.asyncio
async def test_quotes_do_not_exempt_new_translation():
    context = await guard.prepare_context({"s": ENGLISH})
    result = await guard.evaluate_language_integrity(context, [guard.GeneratedText("f", '"' + FRENCH + '"', ("s",))])
    assert result.mismatches


def test_quoted_language_does_not_license_translating_the_account():
    quote = "La réunion commence demain matin avec toute notre équipe."
    context = guard._prepare_context_sync({"source": ENGLISH + f' The quoted message was "{quote}".'})
    result = guard._evaluate_sync(
        context,
        [
            guard.GeneratedText(
                "translation",
                "L’équipe des opérations a terminé la révision et les tests. La prochaine version conservera les documents sources et signalera les échecs pour examen.",
                ("source",),
            )
        ],
    )
    assert guard.enforcement_failures(result, guard.LanguageIntegrityMode.REJECT)


def test_genuine_multilingual_source_and_output_remain_valid():
    french = "L’équipe prépare les documents pour la prochaine réunion et vérifie les résultats avec les responsables du projet."
    context = guard._prepare_context_sync({"source": ENGLISH + "\n" + french})
    output = "The team completed the review and will preserve the original source records for future work.\nL’équipe vérifie les résultats avec les responsables et prépare les documents pour la prochaine réunion."
    result = guard._evaluate_sync(context, [guard.GeneratedText("multilingual", output, ("source",))])
    assert not guard.enforcement_failures(result, guard.LanguageIntegrityMode.REJECT)
