"""Conservative, asynchronous checks for LLM-generated language drift.

This guard deliberately abstains on short, ambiguous, or materially multilingual
source text. It is a safety net after prompt-level language instructions, not a
claim that statistical language identification is infallible.
"""

from __future__ import annotations

import asyncio
import logging
import re
import threading
import unicodedata
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

_CONFIDENCE_FLOOR = 0.80
_MARGIN_FLOOR = 0.30
_MIN_LETTERS = 20
_MIXED_MIN_LETTERS = 100
_MIXED_MIN_FOREIGN_LETTERS = 40
_MIXED_MIN_FOREIGN_SHARE = 0.20
_ABSTAIN_LANGUAGES = frozenset({"zxx"})
_SEGMENT_BOUNDARY = re.compile(r"(?:\n+|(?<=[.!?。！？])\s+)")
_WORD = re.compile(r"[^\W\d_]+", re.UNICODE)
logger = logging.getLogger(__name__)
# Independent confirmation for same-script mismatches. Statistical language ID
# can be confidently wrong on names and technical prose, so Latin-to-Latin
# blocking requires characteristic function words on both sides.
_LANGUAGE_MARKERS: dict[str, frozenset[str]] = {
    "en": frozenset(
        {"a", "an", "and", "are", "as", "at", "for", "from", "in", "is", "of", "on", "that", "the", "to", "was", "with"}
    ),
    "de": frozenset({"das", "dem", "den", "der", "des", "die", "ein", "eine", "für", "in", "ist", "mit", "und", "zu"}),
    "es": frozenset(
        {"al", "con", "de", "del", "el", "en", "la", "las", "los", "para", "por", "que", "se", "un", "una", "y"}
    ),
    "fr": frozenset({"au", "avec", "de", "des", "du", "en", "et", "la", "le", "les", "pour", "que", "un", "une"}),
    "it": frozenset(
        {"che", "con", "del", "della", "di", "e", "gli", "i", "il", "in", "la", "le", "lo", "per", "un", "una"}
    ),
    "nl": frozenset({"de", "een", "en", "het", "in", "is", "met", "op", "te", "van", "voor"}),
    "pt": frozenset({"a", "as", "com", "da", "de", "do", "e", "em", "o", "os", "para", "por", "que", "um", "uma"}),
}


class LanguageIntegrityMode(StrEnum):
    """Configured response to a confidently detected generated-language mismatch."""

    OFF = "off"
    OBSERVE = "observe"
    RETRY = "retry"
    REJECT = "reject"


@dataclass(frozen=True, slots=True)
class LanguageProfile:
    language: str
    confidence: float
    margin: float
    letter_count: int
    dominant_script: str
    mixed: bool = False

    @property
    def actionable(self) -> bool:
        return (
            not self.mixed
            and self.language not in _ABSTAIN_LANGUAGES
            and self.letter_count >= _MIN_LETTERS
            and self.confidence >= _CONFIDENCE_FLOOR
            and self.margin >= _MARGIN_FLOOR
        )


@dataclass(frozen=True, slots=True)
class GeneratedText:
    """One generated value and the source-text group it must preserve."""

    key: str
    text: str
    source_keys: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class LanguageContext:
    """Source profiles prepared once, before generation and corrective retry."""

    source_texts: dict[str, str]
    source_profiles: dict[str, LanguageProfile]


@dataclass(frozen=True, slots=True)
class LanguageMismatch:
    key: str
    source_language: str
    generated_language: str


@dataclass(frozen=True, slots=True)
class LanguageCheckResult:
    """Bounded validation result that distinguishes checks from abstentions."""

    mismatches: tuple[LanguageMismatch, ...]
    checked: int
    abstained: int


class LanguageIntegrityError(RuntimeError):
    retryable = False


class GeneratedLanguageMismatch(LanguageIntegrityError):
    """Strict-mode terminal failure after corrective regeneration also drifted."""

    retryable = False

    def __init__(self, mismatches: Sequence[LanguageMismatch]):
        self.mismatches = tuple(mismatches)
        pairs = sorted({f"{item.source_language}->{item.generated_language}" for item in mismatches})
        super().__init__(f"generated language differs from source language ({', '.join(pairs)})")


class LanguageIntegrityUnavailable(LanguageIntegrityError):
    """Raised in strict mode when the detector itself cannot run."""


def configured_mode(config: Any) -> LanguageIntegrityMode:
    raw = getattr(config, "llm_language_integrity", LanguageIntegrityMode.OBSERVE)
    if not isinstance(raw, str):
        return LanguageIntegrityMode.OBSERVE
    return LanguageIntegrityMode(raw.lower())


def should_check(config: Any) -> bool:
    """Return whether source-language validation applies to this operation."""

    return configured_mode(config) is not LanguageIntegrityMode.OFF and not getattr(config, "llm_output_language", None)


def build_retry_instruction(mismatches: Sequence[LanguageMismatch]) -> str:
    keys = ", ".join(sorted({item.key for item in mismatches}))
    return (
        "\n\nLANGUAGE CORRECTION: The previous generated text switched away from the source's language. "
        f"Regenerate the structured response and correct only these text fields: {keys}. "
        "Each corrected field must preserve the language of its own cited source. Keep unaffected fields in "
        "their own source-specific languages; a multilingual response is valid. Preserve names, identifiers, "
        "URLs, quoted text, and required JSON keys exactly."
    )


def record_outcome(*, stage: str, mode: LanguageIntegrityMode, outcome: str) -> None:
    """Record a text-free, bounded-cardinality language-integrity result."""

    from hindsight_api.metrics import get_metrics_collector

    get_metrics_collector().record_language_integrity(stage=stage, mode=mode.value, outcome=outcome)


_identifier: Any | None = None
_identifier_init_lock = threading.Lock()
_classification_lock = threading.Lock()


def _get_identifier() -> Any:
    global _identifier
    if _identifier is None:
        with _identifier_init_lock:
            if _identifier is None:
                import py3langid.langid as langid

                assert langid.__file__ is not None
                model_path = Path(langid.__file__).resolve().parent / langid.MODEL_FILE
                _identifier = langid.LanguageIdentifier.from_modelpath(str(model_path), norm_probs=True)
    return _identifier


def _letter_count(text: str) -> int:
    return sum(unicodedata.category(char).startswith("L") for char in text)


def _letter_script(char: str) -> str:
    name = unicodedata.name(char, "")
    script = next(
        (
            candidate
            for candidate in (
                "LATIN",
                "CYRILLIC",
                "GREEK",
                "ARABIC",
                "HEBREW",
                "DEVANAGARI",
                "BENGALI",
                "GEORGIAN",
                "ARMENIAN",
                "HIRAGANA",
                "KATAKANA",
                "HANGUL",
                "THAI",
            )
            if candidate in name
        ),
        "HAN" if "CJK UNIFIED IDEOGRAPH" in name else "OTHER",
    )
    return "EAST_ASIAN" if script in {"HAN", "HIRAGANA", "KATAKANA", "HANGUL"} else script


def _script_counts(text: str) -> Counter[str]:
    counts: Counter[str] = Counter()
    for char in text:
        if not unicodedata.category(char).startswith("L"):
            continue
        counts[_letter_script(char)] += 1
    return counts


def _dominant_script(text: str) -> str:
    counts = _script_counts(text)
    return counts.most_common(1)[0][0] if counts else "OTHER"


def _script_letters(text: str, script: str) -> str:
    return "".join(
        char.casefold()
        for char in text
        if unicodedata.category(char).startswith("L") and _letter_script(char) == script
    )


def _preserves_foreign_script_text(source_text: str, generated_text: str, generated_script: str) -> bool:
    """Return true when generated foreign-script text is copied from the source."""

    generated_letters = _script_letters(generated_text, generated_script)
    if len(generated_letters) < _MIN_LETTERS:
        return False
    return generated_letters in _script_letters(source_text, generated_script)


def _marker_count(text: str, language: str) -> int:
    markers = _LANGUAGE_MARKERS.get(language)
    if markers is None:
        return 0
    return sum(word.casefold() in markers for word in _WORD.findall(text))


def _same_script_mismatch_confirmed(
    *, source_text: str, generated_text: str, source_language: str, generated_language: str
) -> bool:
    source_expected = _marker_count(source_text, source_language)
    source_other = _marker_count(source_text, generated_language)
    generated_expected = _marker_count(generated_text, generated_language)
    generated_other = _marker_count(generated_text, source_language)
    return (
        source_expected >= 2
        and generated_expected >= 2
        and source_expected > source_other
        and generated_expected > generated_other
    )


def _rank(text: str) -> tuple[str, float, float]:
    ranked = _get_identifier().rank(text)[:2]
    first_language, first_score = str(ranked[0][0]), float(ranked[0][1])
    second_score = float(ranked[1][1]) if len(ranked) > 1 else 0.0
    return first_language, first_score, first_score - second_score


def _materially_mixed(text: str, primary_language: str) -> bool:
    scripts = _script_counts(text)
    material_scripts = [count for count in scripts.values() if count >= _MIN_LETTERS]
    if len(material_scripts) >= 2:
        total = sum(material_scripts)
        minority = total - max(material_scripts)
        if minority >= _MIXED_MIN_FOREIGN_LETTERS and minority / total >= _MIXED_MIN_FOREIGN_SHARE:
            return True

    language_letters: Counter[str] = Counter()
    for segment in _SEGMENT_BOUNDARY.split(text):
        letters = _letter_count(segment)
        if letters < 30:
            continue
        language, confidence, margin = _rank(segment)
        if language in _ABSTAIN_LANGUAGES or confidence < _CONFIDENCE_FLOOR or margin < _MARGIN_FLOOR:
            continue
        language_letters[language] += letters
    total = sum(language_letters.values())
    foreign = total - language_letters[primary_language]
    return (
        total >= _MIXED_MIN_LETTERS
        and foreign >= _MIXED_MIN_FOREIGN_LETTERS
        and foreign / total >= _MIXED_MIN_FOREIGN_SHARE
    )


def _profile(text: str, *, source: bool) -> LanguageProfile:
    letters = _letter_count(text)
    if letters < _MIN_LETTERS:
        return LanguageProfile("zxx", 0.0, 0.0, letters, _dominant_script(text))
    language, confidence, margin = _rank(text)
    mixed = source and _materially_mixed(text, language)
    return LanguageProfile(language, confidence, margin, letters, _dominant_script(text), mixed)


def _prepare_context_sync(source_texts: Mapping[str, str]) -> LanguageContext:
    with _classification_lock:
        copied_texts = dict(source_texts)
        profiles = {key: _profile(text, source=True) for key, text in copied_texts.items()}
    return LanguageContext(copied_texts, profiles)


def _expected_source(context: LanguageContext, keys: tuple[str, ...]) -> tuple[LanguageProfile, str] | None:
    profiles = [context.source_profiles[key] for key in keys if key in context.source_profiles]
    actionable = [profile for profile in profiles if profile.actionable]
    if not actionable or len(profiles) != len(keys) or len(actionable) != len(profiles):
        return None
    languages = {profile.language for profile in actionable}
    scripts = {profile.dominant_script for profile in actionable}
    if len(languages) != 1 or len(scripts) != 1:
        return None
    combined_text = "\n".join(context.source_texts[key] for key in keys if key in context.source_texts)
    return actionable[0], combined_text


def _evaluate_sync(context: LanguageContext, generated: Sequence[GeneratedText]) -> LanguageCheckResult:
    # py3langid's NumPy-backed identifier is process-global. Serialize each whole
    # validation batch so concurrent async requests never share mutable classifier
    # internals, while the event loop remains free.
    with _classification_lock:
        mismatches: list[LanguageMismatch] = []
        checked = 0
        abstained = 0
        for item in generated:
            expected = _expected_source(context, item.source_keys)
            if expected is None:
                abstained += 1
                continue
            source_profile, source_text = expected
            generated_profile = _profile(item.text, source=False)
            if not generated_profile.actionable:
                abstained += 1
                continue
            checked += 1
            if generated_profile.language == source_profile.language:
                continue
            same_script = generated_profile.dominant_script == source_profile.dominant_script
            if not same_script and _preserves_foreign_script_text(
                source_text, item.text, generated_profile.dominant_script
            ):
                continue
            if (
                same_script
                and source_profile.dominant_script == "LATIN"
                and not _same_script_mismatch_confirmed(
                    source_text=source_text,
                    generated_text=item.text,
                    source_language=source_profile.language,
                    generated_language=generated_profile.language,
                )
            ):
                continue
            mismatches.append(
                LanguageMismatch(
                    key=item.key,
                    source_language=source_profile.language,
                    generated_language=generated_profile.language,
                )
            )
        return LanguageCheckResult(tuple(mismatches), checked, abstained)


async def prepare_context(source_texts: Mapping[str, str]) -> LanguageContext:
    """Profile source text once without blocking the event loop."""

    return await asyncio.to_thread(_prepare_context_sync, source_texts)


async def find_mismatches(context: LanguageContext, generated: Sequence[GeneratedText]) -> tuple[LanguageMismatch, ...]:
    """Classify one output batch against a prepared source context."""

    return (await evaluate_language_integrity(context, generated)).mismatches


async def evaluate_language_integrity(
    context: LanguageContext, generated: Sequence[GeneratedText]
) -> LanguageCheckResult:
    """Classify outputs and report how many were checked versus abstained."""

    if not context.source_texts or not generated:
        return LanguageCheckResult((), 0, len(generated))
    return await asyncio.to_thread(_evaluate_sync, context, tuple(generated))


async def evaluate_language_integrity_safely(
    context: LanguageContext,
    generated: Sequence[GeneratedText],
    *,
    stage: str,
    mode: LanguageIntegrityMode,
) -> LanguageCheckResult | None:
    """Evaluate outputs, returning None after a fail-open detector error."""

    try:
        return await evaluate_language_integrity(context, generated)
    except Exception as exc:
        logger.exception("Language-integrity output profiling failed; stage=%s mode=%s", stage, mode.value)
        record_outcome(stage=stage, mode=mode, outcome="error")
        if mode is LanguageIntegrityMode.REJECT:
            raise LanguageIntegrityUnavailable("language-integrity output profiling failed") from exc
        return None


async def prepare_context_safely(
    source_texts: Mapping[str, str], *, stage: str, mode: LanguageIntegrityMode
) -> LanguageContext | None:
    """Profile sources, failing open unless the operator selected strict mode."""

    try:
        return await prepare_context(source_texts)
    except Exception as exc:
        logger.exception("Language-integrity source profiling failed; stage=%s mode=%s", stage, mode.value)
        record_outcome(stage=stage, mode=mode, outcome="error")
        if mode is LanguageIntegrityMode.REJECT:
            raise LanguageIntegrityUnavailable("language-integrity source profiling failed") from exc
        return None


async def find_mismatches_safely(
    context: LanguageContext,
    generated: Sequence[GeneratedText],
    *,
    stage: str,
    mode: LanguageIntegrityMode,
) -> tuple[LanguageMismatch, ...]:
    """Validate outputs, failing open unless the operator selected strict mode."""

    result = await evaluate_language_integrity_safely(context, generated, stage=stage, mode=mode)
    return result.mismatches if result is not None else ()


def build_source_instruction(context: LanguageContext, source_keys: Sequence[str]) -> str:
    """Return a prompt-tail directive when all requested sources share one language."""

    if _expected_source(context, tuple(source_keys)) is None:
        return ""
    return (
        "\n\nLANGUAGE INTEGRITY: Preserve the source's language in every generated text field. "
        "Do not translate or switch languages unless the source explicitly requests translation."
    )
