"""Conservative source/output language compatibility checks.

The LLM remains responsible for generating text. This module only rejects a
response when both sides are long enough and the detector is confident that the
generated language is unsupported by its source. Ambiguous, technical, short,
and language-neutral text stays indeterminate rather than becoming a false
failure.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum

from langdetect import DetectorFactory, LangDetectException, detect_langs

_MIN_ANALYZABLE_CHARS = 48
_MIN_CONFIDENCE = 0.92
_MIN_MARGIN = 0.20
_MIN_MIXED_SHARE = 0.20

_CODE_BLOCK_RE = re.compile(r"```.*?```", re.DOTALL)
_INLINE_CODE_RE = re.compile(r"`[^`]*`")
_URL_RE = re.compile(r"(?:https?://|www\.)\S+", re.IGNORECASE)
_UUID_RE = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.IGNORECASE)
_IDENTIFIER_RE = re.compile(r"\b(?=\w*[A-Z_0-9])(?=\w*[_0-9])[A-Za-z_][A-Za-z0-9_.:/-]{5,}\b")
_SEGMENT_RE = re.compile(r"(?:\n\s*\n|(?<=[.!?。！？])\s+)")
_JAPANESE_RE = re.compile(r"[\u3040-\u30ff]")
_KOREAN_RE = re.compile(r"[\uac00-\ud7af]")
_WORD_RE = re.compile(r"[^\W\d_]+", re.UNICODE)
_ENGLISH_MARKERS = frozenset(
    {
        "and",
        "are",
        "by",
        "for",
        "from",
        "is",
        "of",
        "on",
        "or",
        "that",
        "the",
        "this",
        "to",
        "was",
        "were",
        "with",
    }
)

# langdetect otherwise samples features randomly. A fixed seed makes the guard
# repeatable across workers and test runs.
DetectorFactory.seed = 0

_LANGUAGE_NAMES = {
    "arabic": "ar",
    "bulgarian": "bg",
    "catalan": "ca",
    "chinese": "zh-cn",
    "croatian": "hr",
    "czech": "cs",
    "danish": "da",
    "dutch": "nl",
    "english": "en",
    "estonian": "et",
    "finnish": "fi",
    "french": "fr",
    "german": "de",
    "greek": "el",
    "hebrew": "he",
    "hindi": "hi",
    "hungarian": "hu",
    "indonesian": "id",
    "italian": "it",
    "japanese": "ja",
    "korean": "ko",
    "latvian": "lv",
    "lithuanian": "lt",
    "norwegian": "no",
    "persian": "fa",
    "polish": "pl",
    "portuguese": "pt",
    "romanian": "ro",
    "russian": "ru",
    "slovak": "sk",
    "slovenian": "sl",
    "spanish": "es",
    "swahili": "sw",
    "swedish": "sv",
    "thai": "th",
    "turkish": "tr",
    "ukrainian": "uk",
    "urdu": "ur",
    "vietnamese": "vi",
}
_SUPPORTED_LANGUAGE_CODES = frozenset({*_LANGUAGE_NAMES.values(), "zh"})


class LanguageValidationOutcome(str, Enum):
    MATCH = "match"
    MISMATCH = "mismatch"
    INDETERMINATE = "indeterminate"


class GeneratedLanguageMismatch(RuntimeError):
    """A model response confidently changed language after one correction."""

    retryable = False


@dataclass(frozen=True)
class LanguageProfile:
    primary: str | None
    allowed: frozenset[str]
    confidence: float
    margin: float
    analyzable_chars: int
    mixed: bool


@dataclass(frozen=True)
class LanguageValidationResult:
    outcome: LanguageValidationOutcome
    expected_languages: frozenset[str]
    output_language: str | None
    source_profile: LanguageProfile
    output_profile: LanguageProfile


def _normalize_code(code: str) -> str:
    return "zh" if code in {"zh-cn", "zh-tw"} else code


def _clean(text: str) -> str:
    text = _CODE_BLOCK_RE.sub(" ", text)
    text = _INLINE_CODE_RE.sub(" ", text)
    text = _URL_RE.sub(" ", text)
    text = _UUID_RE.sub(" ", text)
    text = _IDENTIFIER_RE.sub(" ", text)
    return " ".join(text.split())


def _analyzable_chars(text: str) -> int:
    return sum(char.isalpha() for char in text)


def _classify(text: str) -> tuple[str | None, float, float]:
    words = [word.casefold() for word in _WORD_RE.findall(text)]
    english_markers = [word for word in words if word in _ENGLISH_MARKERS]
    if len(english_markers) >= 3 and len(set(english_markers)) >= 3:
        return "en", 1.0, 1.0
    try:
        values = detect_langs(text)
    except LangDetectException:
        return None, 0.0, 0.0
    if not values:
        return None, 0.0, 0.0
    top = values[0]
    runner_up = values[1].prob if len(values) > 1 else 0.0
    return _normalize_code(top.lang), float(top.prob), float(top.prob - runner_up)


def _confident(code: str | None, confidence: float, margin: float, chars: int) -> bool:
    return bool(
        code
        and chars >= _MIN_ANALYZABLE_CHARS
        and confidence >= _MIN_CONFIDENCE
        and margin >= _MIN_MARGIN
    )


def profile_language(text: str) -> LanguageProfile:
    cleaned = _clean(text)
    chars = _analyzable_chars(cleaned)
    # Kana and Hangul are language-specific enough to classify short OCR safely;
    # Latin-script prose needs the normal conservative length/confidence gates.
    for script, code in ((_JAPANESE_RE, "ja"), (_KOREAN_RE, "ko")):
        if len(script.findall(cleaned)) >= 4:
            return LanguageProfile(code, frozenset({code}), 1.0, 1.0, chars, False)
    if chars < _MIN_ANALYZABLE_CHARS:
        return LanguageProfile(None, frozenset(), 0.0, 0.0, chars, False)
    words = [word.casefold() for word in _WORD_RE.findall(cleaned)]
    if len(words) >= 20 and len(set(words)) / len(words) < 0.20:
        return LanguageProfile(None, frozenset(), 0.0, 0.0, chars, False)

    primary, confidence, margin = _classify(cleaned)

    segments = [_clean(segment) for segment in _SEGMENT_RE.split(text)]
    eligible_segments = [(segment, _analyzable_chars(segment)) for segment in segments]
    eligible_segments = [(segment, count) for segment, count in eligible_segments if count >= _MIN_ANALYZABLE_CHARS]
    if len(eligible_segments) > 1:
        weighted: dict[str, int] = {}
        segment_scores: list[tuple[float, float]] = []
        for segment, count in eligible_segments:
            code, segment_confidence, segment_margin = _classify(segment)
            if _confident(code, segment_confidence, segment_margin, count):
                assert code is not None
                weighted[code] = weighted.get(code, 0) + count
                segment_scores.append((segment_confidence, segment_margin))
        if weighted:
            classified_chars = sum(weighted.values())
            allowed = frozenset(
                code for code, count in weighted.items() if count / classified_chars >= _MIN_MIXED_SHARE
            )
            primary = max(weighted, key=lambda code: weighted[code])
            confidence = min(score[0] for score in segment_scores)
            margin = min(score[1] for score in segment_scores)
            return LanguageProfile(primary, allowed, confidence, margin, chars, len(allowed) > 1)

    if not _confident(primary, confidence, margin, chars):
        return LanguageProfile(None, frozenset(), confidence, margin, chars, False)
    assert primary is not None
    return LanguageProfile(primary, frozenset({primary}), confidence, margin, chars, False)


def _configured_language_code(output_language: str) -> str | None:
    normalized = output_language.strip().casefold().replace("-", "_").replace(" ", "_")
    parts = normalized.split("_")
    # langdetect reports base ISO-639-1 codes, not locale tags. Resolve the
    # base first so `en-US` and `pt-BR` compare as `en` and `pt`.
    candidates = (parts[0], normalized, parts[-1])
    for candidate in candidates:
        code = _LANGUAGE_NAMES.get(candidate, candidate if candidate in _SUPPORTED_LANGUAGE_CODES else None)
        if code:
            return _normalize_code(code.replace("_", "-"))
    return None


def build_language_retry_instruction(
    *,
    output_language: str | None,
    expected_languages: list[str] | tuple[str, ...],
    actual_languages: list[str] | tuple[str, ...],
    per_source: bool = False,
) -> str:
    """Build a correction that cannot contradict an explicit output language."""
    expected = ",".join(expected_languages)
    actual = ",".join(actual_languages)
    prefix = (
        "\n\nCORRECTION: Your previous response was rejected because its generated "
        f"language ({actual}) did not match the required language ({expected}). "
    )
    if output_language:
        return (
            prefix
            + f"Regenerate the entire structured response in the configured output language: {output_language}. "
            "Translate source prose when required by that setting, while preserving proper nouns, identifiers, "
            "commands, paths, and quotations exactly."
        )
    per_source_note = " Preserve the language associated with each source_fact_ids set." if per_source else ""
    return (
        prefix
        + "Regenerate the entire structured response in the source language. Do not translate the source."
        + per_source_note
        + " Preserve proper nouns, identifiers, commands, paths, and quotations exactly."
    )


def validate_output_language(
    *,
    source_text: str,
    output_text: str,
    output_language: str | None = None,
) -> LanguageValidationResult:
    source = profile_language(source_text)
    output = profile_language(output_text)

    if output_language:
        configured = _configured_language_code(output_language)
        expected = frozenset({configured}) if configured else frozenset()
    else:
        expected = source.allowed

    if not expected or output.primary is None:
        outcome = LanguageValidationOutcome.INDETERMINATE
    elif not output.allowed.issubset(expected):
        outcome = LanguageValidationOutcome.MISMATCH
    else:
        outcome = LanguageValidationOutcome.MATCH

    return LanguageValidationResult(
        outcome=outcome,
        expected_languages=expected,
        output_language=output.primary,
        source_profile=source,
        output_profile=output,
    )


def record_language_validation(stage: str, outcome: str, llm_config) -> None:
    """Emit bounded, text-free validation telemetry without affecting work."""
    try:
        from ..metrics import get_metrics_collector

        get_metrics_collector().record_language_validation(
            stage=stage,
            outcome=outcome,
            provider=str(getattr(llm_config, "provider", "unknown")),
            model=str(getattr(llm_config, "model", "unknown")),
        )
    except Exception:
        # Metrics must never turn an indeterminate check into an ingestion failure.
        pass


def should_validate_language(llm_config) -> bool:
    """The deterministic test provider does not generate source-faithful prose."""
    return str(getattr(llm_config, "provider", "")).casefold() != "mock"
