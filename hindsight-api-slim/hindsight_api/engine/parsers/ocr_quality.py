"""Deterministic admission checks for OCR text before semantic extraction."""

import json
import re
from collections import Counter
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import TypedDict, TypeGuard

_IMAGE_EXTENSIONS = {".bmp", ".gif", ".heic", ".heif", ".jpeg", ".jpg", ".png", ".tif", ".tiff", ".webp"}
_UNCLEAR_PATTERN = re.compile(r"\[\s*unclear\s*\]", re.IGNORECASE)
_TOKEN_PATTERN = re.compile(r"[^\W_]+(?:['’.-][^\W_]+)*", re.UNICODE)
_TIMESTAMP_PATTERN = re.compile(r"(?:[01]?\d|2[0-3]):[0-5]\d(?:\s*[ap]m)?", re.IGNORECASE)
_FENCED_BLOCK_PATTERN = re.compile(r"\A```(?P<language>[\w+-]*)\s*\n?(?P<body>.*?)\n?```\Z", re.DOTALL)
_REFUSAL_PATTERNS = (
    re.compile(
        r"(?:based on the image provided )?(?:there is )?no (?:visible |readable |legible )?text"
        r"(?: (?:(?:is|was) (?:visible|present|found|detected)|visible|present|found|detected))?"
        r"(?: in (?:the (?:provided )?|this |provided )?image(?: provided)?)?"
        r"(?: to transcribe)?"
    ),
    re.compile(
        r"(?:based on the image provided )?(?:there is )?no (?:visible |readable |legible )?text"
        r" to transcribe in (?:the (?:provided )?|this |provided )?image(?: provided)?"
    ),
    re.compile(
        r"(?:the (?:provided )?|this )image (?:contains no|does not contain any) "
        r"(?:(?:visible|readable|legible)(?: or (?:visible|readable|legible))* )?text(?: to transcribe)?"
    ),
    re.compile(
        r"i did not find any (?:visible |readable |legible )text"
        r"(?: in (?:the (?:provided )?|this |provided )?image)?"
    ),
    re.compile(
        r"i (?:cannot|can't) fulfill this request because there is no (?:visible |readable |legible )?text"
        r"(?: to transcribe)? in (?:the )?image provided"
    ),
    re.compile(r"i (?:cannot|can't) fulfill this request i am not able to transcribe text from images(?: of people)?"),
)
_REFUSAL_PREFIXES = (
    "i'm sorry but ",
    "i am sorry but ",
    "i'm sorry ",
    "i am sorry ",
    "sorry but ",
    "sorry ",
)
_REFUSALS = {
    "i am unable to read the image",
    "i am unable to read this image",
    "i can't extract text from the image",
    "i can't extract text from this image",
    "i can't read the image",
    "i can't read this image",
    "i cannot extract text from the image",
    "i cannot extract text from this image",
    "i cannot read the image",
    "i cannot read this image",
    "no legible text",
    "no readable text",
    "no text",
    "no text detected",
    "no text found",
    "no text is visible in the image",
    "no text is visible in the provided image",
    "no text is visible in this image",
    "no text visible",
    "no visible text",
    "no visible text in the image",
    "no visible text in this image",
    "there is no visible text",
    "there is no visible text in the image",
    "there is no visible text in the image to transcribe",
    "there is no visible text in this image",
    "unable to read the image",
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


class _OcrJsonPayload(TypedDict):
    text: str


def is_image_input(filename: str, content_type: str | None = None) -> bool:
    """Return whether a conversion result came from an image input."""
    if content_type and content_type.casefold().split(";", 1)[0].strip().startswith("image/"):
        return True
    return Path(filename).suffix.casefold() in _IMAGE_EXTENSIONS


def evaluate_ocr_quality(content: str) -> OcrQualityResult:
    """Reject only deterministic high-confidence OCR failure modes."""
    normalized_content = _strip_ocr_wrappers(content)
    normalized_refusal = _normalize_phrase(normalized_content)
    unclear_spans = _UNCLEAR_PATTERN.findall(normalized_content)
    uncertainty_character_count = sum(len(span) for span in unclear_spans) + normalized_content.count("\ufffd")
    measurable_text = _UNCLEAR_PATTERN.sub(" ", normalized_content).replace("\ufffd", " ")
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
    ui_chrome_ratio = _ui_chrome_ratio(normalized_content)

    features = OcrQualityFeatures(
        normalized_character_count=normalized_character_count,
        word_count=token_count,
        unique_word_count=unique_token_count,
        alphabetic_ratio=alphabetic_count / normalized_character_count if normalized_character_count else 0.0,
        uncertainty_ratio=uncertainty_ratio,
        repetition_ratio=repetition_ratio,
        ui_chrome_ratio=ui_chrome_ratio,
    )

    # Sparse OCR can be legitimate (for example, an elevator floor, grade, one-time
    # code, or error code), so alphanumeric content is preserved unless another
    # dominant, high-confidence failure signal applies. UI chrome rejects only when
    # every nonempty line is a known control or timestamp, preserving substantive lines.
    reason: OcrQualityReason | None = None
    if _is_refusal(normalized_refusal):
        reason = OcrQualityReason.REFUSAL
    elif (
        uncertainty_character_count
        and uncertainty_ratio >= 0.5
        and (
            unique_token_count <= 1
            or (uncertainty_ratio >= 0.8 and normalized_character_count < 4)
            or (token_count >= 6 and repetition_ratio >= 0.3)
        )
    ):
        reason = OcrQualityReason.EXCESSIVE_UNCERTAINTY
    elif normalized_character_count == 0:
        reason = OcrQualityReason.NO_MEANINGFUL_TEXT
    elif token_count >= 8 and repetition_ratio >= 0.8:
        reason = OcrQualityReason.REPETITION
    elif ui_chrome_ratio == 1.0:
        reason = OcrQualityReason.UI_CHROME

    return OcrQualityResult(accepted=reason is None, reason=reason, features=features)


def _strip_ocr_wrappers(content: str) -> str:
    """Remove inert wrappers emitted by older OCR prompts before evaluation."""
    normalized = re.sub(r"^\s*#{1,6}\s*description\s*:\s*", "", content, count=1, flags=re.IGNORECASE).strip()
    fenced = _FENCED_BLOCK_PATTERN.fullmatch(normalized)
    if not fenced:
        return normalized

    body = fenced.group("body").strip()
    if fenced.group("language").casefold() != "json":
        return body

    try:
        payload: object = json.loads(body)
    except json.JSONDecodeError:
        return body
    if _is_ocr_json_payload(payload):
        return payload["text"].strip()
    return body


def _is_ocr_json_payload(payload: object) -> TypeGuard[_OcrJsonPayload]:
    """Validate the known JSON wrapper before treating it as typed OCR."""
    return isinstance(payload, dict) and isinstance(payload.get("text"), str)


def _is_refusal(normalized_phrase: str) -> bool:
    """Recognize short, standalone OCR refusals without rejecting mixed content."""
    if normalized_phrase in _REFUSALS:
        return True
    if len(normalized_phrase) > 240:
        return False
    return any(pattern.fullmatch(normalized_phrase) for pattern in _REFUSAL_PATTERNS)


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
