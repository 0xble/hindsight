"""Regression tests for rejecting unusable OCR before retention."""

import pytest

from hindsight_api.engine.parsers import FileParser, FileParserRegistry, LowQualityOcrError
from hindsight_api.engine.parsers.ocr_quality import OcrQualityReason, evaluate_ocr_quality


class StaticParser(FileParser):
    """Parser stub that returns one deterministic result."""

    def __init__(self, parser_name: str, content: str):
        self._parser_name = parser_name
        self._content = content
        self.calls = 0

    async def convert(self, file_data: bytes, filename: str) -> str:
        self.calls += 1
        return self._content

    def name(self) -> str:
        return self._parser_name


@pytest.mark.parametrize(
    ("content", "reason"),
    [
        ("No visible text", OcrQualityReason.REFUSAL),
        ("I'm sorry, but I can't read this image.", OcrQualityReason.REFUSAL),
        ("[unclear]\n[unclear]\n[unclear]", OcrQualityReason.EXCESSIVE_UNCERTAINTY),
        ("� � �", OcrQualityReason.EXCESSIVE_UNCERTAINTY),
        ("---", OcrQualityReason.NO_MEANINGFUL_TEXT),
        ("9:26\nBack\nNew Chat\nMessage", OcrQualityReason.UI_CHROME),
        ("9:26\nBack\nUnknown", OcrQualityReason.UI_CHROME),
        ("word word word word word word word word word word", OcrQualityReason.REPETITION),
    ],
)
def test_rejects_unusable_ocr(content: str, reason: OcrQualityReason):
    result = evaluate_ocr_quality(content)

    assert result.accepted is False
    assert result.reason is reason


@pytest.mark.parametrize(
    "content",
    [
        "829104",
        "503\nService unavailable",
        "Order total: $19.95\nCard: [unclear] 4242",
        "I can't read this image\nAccount balance: $100.00",
        "Back pain started after the flight.",
        "这是一个有效的中文句子，其中只有一个�字符",
        "�supercalifragilisticexpialidocious",
    ],
)
def test_accepts_legitimate_sparse_or_partially_uncertain_ocr(content: str):
    result = evaluate_ocr_quality(content)

    assert result.accepted is True
    assert result.reason is None


@pytest.mark.asyncio
async def test_low_quality_image_output_falls_back_to_next_parser():
    weak = StaticParser("weak", "No visible text")
    useful = StaticParser("useful", "Account balance: $100.00")
    registry = FileParserRegistry()
    registry.register(weak)
    registry.register(useful)

    result = await registry.convert_with_fallback(
        ["weak", "useful"],
        b"image bytes",
        "upload",
        "image/png",
    )

    assert result.content == "Account balance: $100.00"
    assert result.parser_name == "useful"
    assert weak.calls == 1
    assert useful.calls == 1


@pytest.mark.asyncio
async def test_all_low_quality_image_outputs_fail_without_echoing_ocr_body(caplog):
    registry = FileParserRegistry()
    registry.register(StaticParser("weak", "No visible text"))

    with pytest.raises(LowQualityOcrError) as exc_info:
        await registry.convert_with_fallback(
            ["weak"],
            b"image bytes",
            "statement.png",
            None,
        )

    assert exc_info.value.reason is OcrQualityReason.REFUSAL
    assert "No visible text" not in str(exc_info.value)
    assert "No visible text" not in caplog.text
    assert OcrQualityReason.REFUSAL.value in caplog.text


@pytest.mark.asyncio
async def test_non_image_content_is_not_subject_to_ocr_quality_gate():
    registry = FileParserRegistry()
    registry.register(StaticParser("text", "No visible text"))

    result = await registry.convert_with_fallback(
        ["text"],
        b"No visible text",
        "notes.txt",
        "text/plain",
    )

    assert result.content == "No visible text"
    assert result.parser_name == "text"
