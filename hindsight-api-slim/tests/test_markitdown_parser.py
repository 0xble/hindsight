"""Unit tests for the markitdown file parser."""

import codecs
import io
import json

import pytest

from hindsight_api.engine.parsers.markitdown import MarkitdownParser


@pytest.fixture
def parser() -> MarkitdownParser:
    return MarkitdownParser()


@pytest.fixture
def raster_ocr(monkeypatch):
    import markitdown.converters._image_converter as images
    from PIL import Image

    monkeypatch.setattr(
        images, "exiftool_metadata", lambda *args, **kwargs: {"ImageSize": "20x20", "Description": "Synthetic metadata"}
    )
    png = io.BytesIO()
    Image.new("RGB", (20, 20), "white").save(png, format="PNG")
    parser = MarkitdownParser(
        ocr_enabled=True, ocr_api_key="synthetic-key", ocr_base_url="http://127.0.0.1:1/v1", ocr_model="existing-model"
    )

    def configure(description):
        monkeypatch.setattr(images.ImageConverter, "_get_llm_description", lambda *args, **kwargs: description)
        return parser, png.getvalue()

    return configure


@pytest.mark.parametrize("description", ["No visible text", "I am unable to read the image.", "", None])
async def test_raster_ocr_metadata_cannot_admit_empty_or_refused_description(raster_ocr, description):
    from hindsight_api.engine.parsers import FileParserRegistry, LowQualityOcrError, NoExtractableContentError

    parser, png = raster_ocr(description)
    registry = FileParserRegistry()
    registry.register(parser)
    error = LowQualityOcrError if description and description.startswith("I am") else NoExtractableContentError
    with pytest.raises(error):
        await registry.convert_with_fallback(["markitdown"], png, "source.png", "image/png")


@pytest.mark.parametrize("description", ["No visible text", "I am unable to read the image."])
async def test_raster_ocr_metadata_rejection_tries_fallback(raster_ocr, description):
    from hindsight_api.engine.parsers import FileParserRegistry
    from tests.test_ocr_quality import StaticParser

    parser, png = raster_ocr(description)
    registry = FileParserRegistry()
    registry.register(parser)
    fallback = StaticParser("fallback", "Alice completed all checks and preserved the original files.")
    registry.register(fallback)
    result = await registry.convert_with_fallback(["markitdown", "fallback"], png, "source.png", "image/png")
    assert result.parser_name == "fallback"
    assert result.content == fallback._content
    assert fallback.calls == 1


async def test_raster_ocr_valid_description_preserves_string_evidence(raster_ocr):
    from hindsight_api.engine.parsers import FileParserRegistry

    description = "Alice completed all checks and preserved the original files."
    parser, png = raster_ocr(description)
    registry = FileParserRegistry()
    registry.register(parser)
    result = await registry.convert_with_fallback(["markitdown"], png, "source.png", "image/png")
    assert result.content == description
    assert result.parser_name == "markitdown"


@pytest.mark.parametrize("extension", [".png", ".jpg", ".jpeg"])
@pytest.mark.parametrize(
    "description",
    [
        "No visible text",
        "I am unable to read the image.",
        "",
        None,
        "Alice completed all checks and preserved the original files.",
    ],
)
async def test_raster_ocr_http_failure_cannot_escape_raw_description_admission(monkeypatch, description, extension):
    """Real SDK + native converter fallback after HTTP 503 must never admit metadata."""
    import markitdown.converters._image_converter as images
    from aiohttp import web
    from PIL import Image

    from hindsight_api.engine.parsers import FileParserRegistry, LowQualityOcrError, NoExtractableContentError
    from tests.aiohttp_stub import stub_server

    monkeypatch.setenv("HINDSIGHT_API_LLM_MAX_RETRIES", "0")
    monkeypatch.setattr(images, "exiftool_metadata", lambda *args, **kwargs: {"ImageSize": "20x20"})
    image = io.BytesIO()
    # Conflicting suffix and native format give MarkItDown two stream guesses.
    # After the first converter fails, every remaining raster route must still
    # use raw-description admission rather than the built-in metadata wrapper.
    Image.new("RGB", (20, 20), "white").save(image, format="JPEG" if extension == ".png" else "PNG")
    filename = "source" + extension
    content_type = "image/png" if extension == ".png" else "image/jpeg"
    requests = []

    async def handler(request):
        requests.append(await request.json())
        if len(requests) == 1:
            return web.json_response({"error": {"message": "synthetic outage"}}, status=503)
        return web.json_response(
            {
                "id": "ocr-test",
                "object": "chat.completion",
                "created": 0,
                "model": "existing-model",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": description},
                        "finish_reason": "stop",
                    }
                ],
            }
        )

    async with stub_server(handler) as base_url:
        parser = MarkitdownParser(
            ocr_enabled=True, ocr_api_key="synthetic-key", ocr_base_url=base_url, ocr_model="existing-model"
        )
        registry = FileParserRegistry()
        registry.register(parser)
        try:
            if description and description.startswith("Alice"):
                result = await registry.convert_with_fallback(["markitdown"], image.getvalue(), filename, content_type)
                assert result.content == description
            else:
                expected = (
                    LowQualityOcrError if description and description.startswith("I am") else NoExtractableContentError
                )
                with pytest.raises(expected):
                    await registry.convert_with_fallback(["markitdown"], image.getvalue(), filename, content_type)
        finally:
            if parser._markitdown is not None:
                parser._markitdown._llm_client.close()
    assert len(requests) == 2


async def test_ocr_enabled_non_image_keeps_native_converter(monkeypatch):
    monkeypatch.setenv("HINDSIGHT_API_LLM_MAX_RETRIES", "0")
    parser = MarkitdownParser(
        ocr_enabled=True, ocr_api_key="synthetic-key", ocr_base_url="http://127.0.0.1:1/v1", ocr_model="existing-model"
    )
    try:
        content = await parser.convert(
            b"<html><body><h1>Original title</h1><p>Original evidence.</p></body></html>", "source.html"
        )
        assert "# Original title" in content
        assert "Original evidence." in content
    finally:
        if parser._markitdown is not None:
            parser._markitdown._llm_client.close()


def _utf8_json_with_ascii_prefix() -> bytes:
    """A JSON file whose first chunk is ASCII but contains multibyte UTF-8 later.

    markitdown samples only the first chunk for charset detection, so this layout
    used to be mis-detected as ASCII and crash the JSON/ipynb converter on the
    first multibyte byte (0xc3).
    """
    payload = {"messages": [{"role": "user", "text": "x" * 6400 + " café à la crème naïve über"}]}
    raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    assert any(b > 127 for b in raw[6000:]), "fixture must have non-ASCII bytes past the sample window"
    return raw


async def test_convert_utf8_json_transcript(parser: MarkitdownParser):
    """A UTF-8 JSON transcript with an ASCII prefix parses without a decode error."""
    file_data = _utf8_json_with_ascii_prefix()

    content = await parser.convert(file_data, "transcript.json")

    assert "café" in content
    assert "crème" in content


async def test_convert_plain_utf8_text(parser: MarkitdownParser):
    """A plain UTF-8 text file with non-ASCII content round-trips."""
    file_data = ("über résumé\n" + "a" * 7000 + "\nfin: naïveté").encode("utf-8")

    content = await parser.convert(file_data, "notes.txt")

    assert "über" in content
    assert "naïveté" in content


@pytest.mark.parametrize(
    ("filename", "text"),
    [("notes.md", "# Title\n\nBody\n"), ("notes.txt", "hello"), ("data.json", '{"a": "Köln"}')],
)
async def test_convert_strips_utf8_bom(parser: MarkitdownParser, filename: str, text: str):
    """A leading UTF-8 byte-order mark is dropped, not kept as U+FEFF."""
    content = await parser.convert(codecs.BOM_UTF8 + text.encode("utf-8"), filename)

    assert content[0] == text[0]


def test_utf8_stream_info_for_text_extension():
    """Text files that are valid UTF-8 get an explicit UTF-8 charset hint."""
    info = MarkitdownParser._utf8_stream_info("über".encode("utf-8"), "a.json")

    assert info is not None
    assert info.charset == "utf-8"


def test_utf8_stream_info_skips_binary_extension():
    """Binary files are left to markitdown's own detection (no hint)."""
    assert MarkitdownParser._utf8_stream_info(b"%PDF-1.4 ...", "a.pdf") is None


def test_utf8_stream_info_skips_non_utf8_text():
    """Non-UTF-8 text falls back to markitdown's detection (no hint)."""
    latin1 = "café".encode("latin-1")  # 0xe9, invalid as standalone UTF-8

    assert MarkitdownParser._utf8_stream_info(latin1, "a.txt") is None


def test_utf8_stream_info_accepts_non_bytes_buffer():
    """file_data may arrive as a buffer-protocol object that is not a Python
    ``bytes`` (e.g. a memoryview or a native/Rust-backed buffer) and therefore
    has no ``.decode``. The UTF-8 probe must coerce via ``bytes()`` instead of
    assuming concrete ``bytes``, else every text file fails to parse with
    ``'...' object has no attribute 'decode'``.
    """
    # memoryview is a buffer-protocol object with no ``.decode`` and, unlike a
    # PEP 688 ``__buffer__`` class, ``bytes(memoryview)`` works on every
    # supported Python version — a portable stand-in for the native buffer the
    # storage layer returns.
    buf = memoryview("über".encode("utf-8"))
    assert not hasattr(buf, "decode")  # precondition: would hit the original AttributeError

    info = MarkitdownParser._utf8_stream_info(buf, "a.txt")

    assert info is not None
    assert info.charset == "utf-8"
