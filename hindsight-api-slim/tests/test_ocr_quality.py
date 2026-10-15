"""Regression tests for rejecting unusable OCR before retention."""

import pytest

from hindsight_api.engine.memory_engine import _file_convert_failure_metadata
from hindsight_api.engine.parsers import FileParser, FileParserRegistry, LowQualityOcrError, NoExtractableContentError
from hindsight_api.engine.parsers.base import UnsupportedFileTypeError
from hindsight_api.engine.parsers.ocr_quality import OcrQualityReason, evaluate_ocr_quality
from tests.test_no_extractable_text import StubParser


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
        ("No visible text", OcrQualityReason.NO_MEANINGFUL_TEXT),
        ("No text visible.", OcrQualityReason.NO_MEANINGFUL_TEXT),
        ("There is no visible text in the image.", OcrQualityReason.NO_MEANINGFUL_TEXT),
        ("# Description: No text is visible in the provided image.", OcrQualityReason.NO_MEANINGFUL_TEXT),
        ("# Description: There is no visible text in the image to transcribe.", OcrQualityReason.NO_MEANINGFUL_TEXT),
        ("# Description: No visible text found in the image.", OcrQualityReason.NO_MEANINGFUL_TEXT),
        (
            "# Description: This image does not contain any visible text to transcribe.",
            OcrQualityReason.NO_MEANINGFUL_TEXT,
        ),
        (
            "# Description: This image does not contain any visible or readable text to transcribe.",
            OcrQualityReason.NO_MEANINGFUL_TEXT,
        ),
        ("# Description: I did not find any visible text in the image.", OcrQualityReason.NO_MEANINGFUL_TEXT),
        ("No visible text is present in the image.", OcrQualityReason.NO_MEANINGFUL_TEXT),
        ("The image contains no visible text to transcribe.", OcrQualityReason.NO_MEANINGFUL_TEXT),
        ("There is no text visible in the provided image.", OcrQualityReason.NO_MEANINGFUL_TEXT),
        ("No text was detected in the provided image.", OcrQualityReason.NO_MEANINGFUL_TEXT),
        ("Based on the image provided, there is no visible text to transcribe.", OcrQualityReason.NO_MEANINGFUL_TEXT),
        (
            "# Description: The provided image does not contain any readable text to transcribe.",
            OcrQualityReason.NO_MEANINGFUL_TEXT,
        ),
        ("This image does not contain any\nvisible text to transcribe.", OcrQualityReason.NO_MEANINGFUL_TEXT),
        ("# Description: [no text]", OcrQualityReason.NO_MEANINGFUL_TEXT),
        ("# Description: ```markdown\n[No visible text]\n```", OcrQualityReason.NO_MEANINGFUL_TEXT),
        (
            "# Description: I am sorry, but I cannot fulfill this request because there is no visible text "
            "to transcribe in the image provided.",
            OcrQualityReason.REFUSAL,
        ),
        (
            "# Description: I'm sorry, I can't fulfill this request. I am not able to transcribe text from "
            "images of people.",
            OcrQualityReason.REFUSAL,
        ),
        ("I am unable to read the image.", OcrQualityReason.REFUSAL),
        ("I'm sorry, but I can't read this image.", OcrQualityReason.REFUSAL),
        ("[unclear]\n[unclear]\n[unclear]", OcrQualityReason.EXCESSIVE_UNCERTAINTY),
        ("� � �", OcrQualityReason.EXCESSIVE_UNCERTAINTY),
        (
            "# Description: Strawberry guest 10 10 1 1 1 End [unclear] Some [unclear] [unclear] [unclear] "
            "[unclear] [unclear] [unclear] [unclear] [unclear]",
            OcrQualityReason.EXCESSIVE_UNCERTAINTY,
        ),
        (
            "A B " + " ".join(["[unclear]"] * 20),
            OcrQualityReason.EXCESSIVE_UNCERTAINTY,
        ),
        ("# Description: [unclear]", OcrQualityReason.EXCESSIVE_UNCERTAINTY),
        ("# Description: ```markdown\n\n```", OcrQualityReason.NO_MEANINGFUL_TEXT),
        ('# Description: ```json\n{"text": ""}\n```', OcrQualityReason.NO_MEANINGFUL_TEXT),
        ("---", OcrQualityReason.NO_MEANINGFUL_TEXT),
        ("9:26\nBack\nNew Chat\nMessage", OcrQualityReason.UI_CHROME),
        ("word word word word word word word word word word", OcrQualityReason.REPETITION),
    ],
)
def test_rejects_unusable_ocr(content: str, reason: OcrQualityReason):
    result = evaluate_ocr_quality(content)

    assert result.accepted is False
    assert result.reason is reason


@pytest.mark.parametrize(
    ("content", "confirmed"),
    [
        ("No visible text", True),
        ("I am unable to read the image", False),
        ("---", False),
        ("No text messages while driving", False),
    ],
)
def test_confirmed_no_text_flag_is_narrow(content: str, confirmed: bool):
    assert evaluate_ocr_quality(content).confirmed_no_text is confirmed


@pytest.mark.parametrize(
    "content",
    [
        "829104",
        "```829104```",
        "```G```",
        "```EXIT```",
        "03",
        "9:26",
        "Back",
        "Today",
        "Message",
        "9:26\n10:30",
        "Back\nBack",
        "G",
        "# Description: 9",
        "Description: X",
        "No text messages while driving",
        "Employees cannot read confidential files.",
        "This policy does not contain prohibited text.",
        "I can't read this image. Account balance: $100.00",
        "503\nService unavailable",
        "Order total: $19.95\nCard: [unclear] 4242",
        "I can't read this image\nAccount balance: $100.00",
        "Back pain started after the flight.",
        "9:26\nBack\nUnknown",
        "9:26\nBack\nAccount balance: $100.00",
        "Today\nBack\nMeeting moved to 3pm",
        "这是一个有效的中文句子，其中只有一个�字符",
        "�supercalifragilisticexpialidocious",
        "# Description: 出口 EXIT [unclear]",
        "# Description: 三田3丁目 Mita 3 MA Building Clinic Dental [unclear] [unclear] [unclear] [unclear]",
        (
            "# Description: Trattoria Roma [unclear] [unclear] Highland Main Street Open daily Pasta Pizza "
            "Wine Coffee Desserts Family Dining Lunch Dinner [unclear] [unclear] [unclear] [unclear] "
            "[unclear] [unclear] [unclear]"
        ),
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
async def test_uncertainty_dominated_image_output_falls_back_to_next_parser():
    weak = StaticParser("weak", "A B " + " ".join(["[unclear]"] * 20))
    useful = StaticParser("useful", "EXIT")
    registry = FileParserRegistry()
    registry.register(weak)
    registry.register(useful)

    result = await registry.convert_with_fallback(
        ["weak", "useful"],
        b"image bytes",
        "upload.png",
        "image/png",
    )

    assert result.content == "EXIT"
    assert result.parser_name == "useful"
    assert weak.calls == 1
    assert useful.calls == 1


@pytest.mark.asyncio
async def test_all_provider_refusals_remain_failures_without_echoing_ocr_body(caplog):
    registry = FileParserRegistry()
    registry.register(StaticParser("weak", "I am unable to read the image"))

    with pytest.raises(LowQualityOcrError) as exc_info:
        await registry.convert_with_fallback(
            ["weak"],
            b"image bytes",
            "statement.png",
            None,
        )

    assert exc_info.value.reason is OcrQualityReason.REFUSAL
    assert "I am unable to read the image" not in str(exc_info.value)
    assert "I am unable to read the image" not in caplog.text
    assert OcrQualityReason.REFUSAL.value in caplog.text


@pytest.mark.asyncio
async def test_confirmed_text_free_image_is_typed_as_no_extractable_content():
    registry = FileParserRegistry()
    registry.register(StaticParser("ocr", "No visible text"))

    with pytest.raises(NoExtractableContentError) as exc_info:
        await registry.convert_with_fallback(
            ["ocr"],
            b"image bytes",
            "statement.png",
            None,
        )

    wrapped = RuntimeError("file failed")
    wrapped.__cause__ = exc_info.value
    assert _file_convert_failure_metadata(wrapped) == {
        "failure_class": "no_extractable_text",
        "failure_reason": "empty_content",
        "parsers": ["ocr"],
    }


@pytest.mark.asyncio
async def test_symbols_only_image_output_remains_low_quality():
    registry = FileParserRegistry()
    registry.register(StaticParser("ocr", "---"))

    with pytest.raises(LowQualityOcrError) as exc_info:
        await registry.convert_with_fallback(["ocr"], b"image bytes", "statement.png", None)

    assert exc_info.value.reason is OcrQualityReason.NO_MEANINGFUL_TEXT
    assert exc_info.value.confirmed_no_text is False


@pytest.mark.asyncio
async def test_pdf_no_text_page_remains_low_quality():
    # The PDF worker transport intentionally omits this raster-only flag, so the
    # non-image registry path must keep a confirmed no-text PDF as low-quality OCR.
    quality = evaluate_ocr_quality("No visible text")
    registry = FileParserRegistry()
    registry.register(StubParser("pdf", LowQualityOcrError("pdf", "scan.pdf", quality)))

    with pytest.raises(LowQualityOcrError) as exc_info:
        await registry.convert_with_fallback(["pdf"], b"pdf bytes", "scan.pdf", "application/pdf")

    assert exc_info.value.reason is OcrQualityReason.NO_MEANINGFUL_TEXT
    assert exc_info.value.confirmed_no_text is True


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


@pytest.mark.asyncio
@pytest.mark.parametrize("transient_first", [True, False])
@pytest.mark.parametrize("error_type", [TimeoutError, ConnectionError, RuntimeError])
async def test_mixed_ocr_chain_preserves_nonterminal_error(transient_first: bool, error_type: type[Exception]):
    registry = FileParserRegistry()
    transient = error_type("provider unavailable")
    registry.register(StubParser("transient", transient))
    registry.register(StaticParser("weak", "No visible text"))
    registry.register(StaticParser("empty", ""))
    chain = ["transient", "weak"] if transient_first else ["weak", "transient"]
    chain.append("empty")

    with pytest.raises(error_type) as caught:
        await registry.convert_with_fallback(chain, b"image", "scan.png")

    assert caught.value is transient
    assert _file_convert_failure_metadata(caught.value) == {}
    wrapped = RuntimeError("file failed")
    wrapped.__cause__ = caught.value
    assert _file_convert_failure_metadata(wrapped) == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("unsupported_first", [True, False])
@pytest.mark.parametrize("trailing_empty", [True, False])
async def test_unsupported_parser_does_not_outrank_capable_parsers_ocr_rejection(
    unsupported_first: bool, trailing_empty: bool
) -> None:
    registry = FileParserRegistry()
    registry.register(StubParser("unsupported", UnsupportedFileTypeError("unsupported format")))
    registry.register(StaticParser("first", "No visible text"))
    registry.register(StaticParser("second", "[unclear]"))
    registry.register(StaticParser("empty", ""))
    chain = ["unsupported", "first", "second"] if unsupported_first else ["first", "second", "unsupported"]
    if trailing_empty:
        chain.append("empty")

    with pytest.raises(LowQualityOcrError) as caught:
        await registry.convert_with_fallback(chain, b"image", "scan.png")

    assert _file_convert_failure_metadata(caught.value) == {
        "failure_class": "low_quality_ocr",
        "failure_reason": OcrQualityReason.EXCESSIVE_UNCERTAINTY.value,
    }


@pytest.mark.asyncio
async def test_low_quality_only_chain_still_has_terminal_ocr_details():
    registry = FileParserRegistry()
    registry.register(StaticParser("first", "No visible text"))
    registry.register(StaticParser("second", "[unclear]"))

    with pytest.raises(LowQualityOcrError) as caught:
        await registry.convert_with_fallback(["first", "second"], b"image", "scan.png")

    assert _file_convert_failure_metadata(caught.value) == {
        "failure_class": "low_quality_ocr",
        "failure_reason": OcrQualityReason.EXCESSIVE_UNCERTAINTY.value,
    }


@pytest.mark.asyncio
async def test_mixed_ocr_chain_can_still_succeed():
    registry = FileParserRegistry()
    registry.register(StubParser("transient", TimeoutError("timeout")))
    registry.register(StaticParser("weak", "No visible text"))
    registry.register(StaticParser("useful", "EXIT"))

    result = await registry.convert_with_fallback(["transient", "weak", "useful"], b"image", "scan.png")

    assert result.content == "EXIT"
    assert result.parser_name == "useful"
