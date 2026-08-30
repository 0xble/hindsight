"""Deterministic admission checks for OCR text before semantic extraction."""

import re
from collections import Counter
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

_IMAGE_EXTENSIONS = {".bmp", ".gif", ".heic", ".heif", ".jpeg", ".jpg", ".png", ".tif", ".tiff", ".webp"}
_UNCLEAR_PATTERN = re.compile(r"\[\s*unclear\s*\]", re.IGNORECASE)
_TOKEN_PATTERN = re.compile(r"[^\W_]+(?:['’.-][^\W_]+)*", re.UNICODE)
_TIMESTAMP_PATTERN = re.compile(r"(?:[01]?\d|2[0-3]):[0-5]\d(?:\s*[ap]m)?", re.IGNORECASE)
_REFUSAL_PREFIXES = (
    "i'm sorry but ",
    "i am sorry but ",
    "i'm sorry ",
    "i am sorry ",
    "sorry but ",
    "sorry ",
)
_REFUSALS = {
    "i am unable to read this image",
    "i can't extract text from this image",
    "i can't read this image",
    "i cannot extract text from this image",
    "i cannot read this image",
    "no legible text",
    "no readable text",
    "no text detected",
    "no text found",
    "no text is visible in the image",
    "no visible text",
    "no visible text in the image",
    "unable to read this image",
}
_UI_CHROME_LINES = {
    "back",
    "cancel",
    "close",
    "done",
    "edit",
    "home",
    "menu",
    "message",
    "more",
    "new chat",
    "next",
    "search",
    "send",
    "settings",
    "share",
    "skip",
    "today",
    "yesterday",
}


class OcrQualityReason(str, Enum):
    """Stable reason codes for rejected OCR."""

    REFUSAL = "refusal_or_no_text_response"
    NO_MEANINGFUL_TEXT = "no_meaningful_text"
    EXCESSIVE_UNCERTAINTY = "excessive_uncertainty"
    REPETITION = "excessive_repetition"
    UI_CHROME = "ui_chrome_only"


@dataclass(frozen=True)
class OcrQualityFeatures:
    """Non-content measurements used to explain an OCR quality decision."""

    normalized_character_count: int
    word_count: int
    unique_word_count: int
    alphabetic_ratio: float
    uncertainty_ratio: float
    repetition_ratio: float
    ui_chrome_ratio: float


@dataclass(frozen=True)
class OcrQualityResult:
    """Admission decision and measurements for one OCR result."""

    accepted: bool
    reason: OcrQualityReason | None
    features: OcrQualityFeatures


class LowQualityOcrError(RuntimeError):
    """Raised when a parser returns nonempty but unusable OCR text."""

    def __init__(self, parser_name: str, filename: str, result: OcrQualityResult):
        if result.reason is None:
            raise ValueError("Rejected OCR must include a reason")
        self.parser_name = parser_name
        self.filename = filename
        self.reason = result.reason
        self.features = result.features
        super().__init__(f"Parser '{parser_name}' returned low-quality OCR for '{filename}' ({result.reason.value})")


def is_image_input(filename: str, content_type: str | None = None) -> bool:
    """Return whether a conversion result came from an image input."""
    if content_type and content_type.casefold().split(";", 1)[0].strip().startswith("image/"):
        return True
    return Path(filename).suffix.casefold() in _IMAGE_EXTENSIONS


def evaluate_ocr_quality(content: str) -> OcrQualityResult:
    """Reject only deterministic high-confidence OCR failure modes."""
    normalized_refusal = _normalize_phrase(content)
    unclear_spans = _UNCLEAR_PATTERN.findall(content)
    uncertainty_character_count = sum(len(span) for span in unclear_spans) + content.count("\ufffd")
    measurable_text = _UNCLEAR_PATTERN.sub(" ", content).replace("\ufffd", " ")
    tokens = [token.casefold() for token in _TOKEN_PATTERN.findall(measurable_text)]
    normalized_character_count = sum(character.isalnum() for character in measurable_text)
    alphabetic_count = sum(character.isalpha() for character in measurable_text)
    token_count = len(tokens)
    unique_token_count = len(set(tokens))
    uncertainty_ratio = (
        uncertainty_character_count / (uncertainty_character_count + normalized_character_count)
        if uncertainty_character_count
        else 0.0
    )
    repetition_ratio = max(Counter(tokens).values()) / token_count if tokens else 0.0
    ui_chrome_ratio = _ui_chrome_ratio(content)

    features = OcrQualityFeatures(
        normalized_character_count=normalized_character_count,
        word_count=token_count,
        unique_word_count=unique_token_count,
        alphabetic_ratio=alphabetic_count / normalized_character_count if normalized_character_count else 0.0,
        uncertainty_ratio=uncertainty_ratio,
        repetition_ratio=repetition_ratio,
        ui_chrome_ratio=ui_chrome_ratio,
    )

    # Sparse OCR can be legitimate (for example, a one-time code or an error code),
    # so length never rejects by itself. Each threshold below requires a dominant,
    # high-confidence failure signal; UI chrome additionally requires short output.
    reason: OcrQualityReason | None = None
    if normalized_refusal in _REFUSALS:
        reason = OcrQualityReason.REFUSAL
    elif uncertainty_character_count and uncertainty_ratio >= 0.5:
        reason = OcrQualityReason.EXCESSIVE_UNCERTAINTY
    elif normalized_character_count == 0:
        reason = OcrQualityReason.NO_MEANINGFUL_TEXT
    elif token_count >= 8 and repetition_ratio >= 0.8:
        reason = OcrQualityReason.REPETITION
    elif token_count <= 10 and ui_chrome_ratio > 0.5:
        reason = OcrQualityReason.UI_CHROME

    return OcrQualityResult(accepted=reason is None, reason=reason, features=features)


def _normalize_phrase(content: str) -> str:
    """Normalize a short model response without changing its meaning."""
    normalized = content.casefold().replace("’", "'")
    normalized = re.sub(r"[^\w']+", " ", normalized, flags=re.UNICODE)
    normalized = " ".join(normalized.split())
    for prefix in _REFUSAL_PREFIXES:
        if normalized.startswith(prefix):
            return normalized.removeprefix(prefix)
    return normalized


def _ui_chrome_ratio(content: str) -> float:
    """Measure lines that are timestamps or common standalone controls."""
    raw_lines = [line.strip() for line in content.splitlines() if line.strip()]
    if not raw_lines:
        return 0.0
    ui_lines = sum(
        _normalize_phrase(line) in _UI_CHROME_LINES or bool(_TIMESTAMP_PATTERN.fullmatch(line)) for line in raw_lines
    )
    return ui_lines / len(raw_lines)
