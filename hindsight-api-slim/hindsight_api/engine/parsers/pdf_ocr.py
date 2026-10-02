"""Bounded PDF page OCR within MarkItDown's existing image-provider route.

PDFium is not thread-safe. Native parsing/rendering and synchronous provider
calls live in a disposable process so cancellation and deadlines stop the work.
"""

from __future__ import annotations

import asyncio
import io
import logging
import math
import multiprocessing
import os
import tempfile
import time
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Literal, TypeAlias, cast

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

from .base import NoExtractableContentError
from .ocr_quality import (
    LowQualityOcrError,
    OcrQualityFeatures,
    OcrQualityReason,
    OcrQualityResult,
    evaluate_ocr_quality,
)

if TYPE_CHECKING:
    from _typeshed import ReadableBuffer
    from openai import OpenAI


@dataclass(frozen=True)
class PdfOcrLimits:
    input_bytes: int = 32 * 1024 * 1024
    pages: int = 20
    page_pixels: int = 10_000_000
    total_pixels: int = 50_000_000
    rendered_page_bytes: int = 16 * 1024 * 1024
    rendered_total_bytes: int = 64 * 1024 * 1024
    output_bytes: int = 1024 * 1024
    seconds: float = 120.0
    request_seconds: float = 30.0
    rss_bytes: int = 1024 * 1024 * 1024
    scale: float = 2.0


PDF_OCR_LIMITS = PdfOcrLimits()


class PdfOcrConfig(BaseModel):
    """Validated existing provider settings transported only in memory."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, hide_input_in_errors=True)

    api_key: str = Field(repr=False)
    base_url: str
    model: str
    prompt: str | None
    default_headers: dict[str, str] | None = Field(repr=False)


class _ProcessResult(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, hide_input_in_errors=True)


class _SuccessResult(_ProcessResult):
    kind: Literal["ok"] = "ok"
    content: str


class _TextSuccessResult(_ProcessResult):
    kind: Literal["text"] = "text"
    content: str


class _QualityResult(_ProcessResult):
    kind: Literal["quality"] = "quality"
    reason: OcrQualityReason
    features: OcrQualityFeatures


class _EmptyResult(_ProcessResult):
    kind: Literal["empty"] = "empty"


class _ErrorResult(_ProcessResult):
    kind: Literal["error"] = "error"
    error_type: str


PdfOcrResult: TypeAlias = Annotated[
    _SuccessResult | _TextSuccessResult | _QualityResult | _EmptyResult | _ErrorResult, Field(discriminator="kind")
]
_RESULT_ADAPTER = TypeAdapter(PdfOcrResult)


class _BoundedBuffer(io.BytesIO):
    def __init__(self, limit: int) -> None:
        super().__init__()
        self.limit = limit

    def write(self, data: ReadableBuffer) -> int:
        if self.tell() + memoryview(data).nbytes > self.limit:
            raise RuntimeError("PDF rendered page exceeds byte limit")
        return super().write(data)


async def convert_pdf(file_data: bytes, filename: str, ocr_config: PdfOcrConfig, limits: PdfOcrLimits) -> str:
    import psutil

    deadline = time.monotonic() + limits.seconds
    # Parent owns all scratch so forced child termination cannot strand files.
    # No credentials or client objects enter argv or files. The existing parser's
    # approved OCR settings travel only over multiprocessing's in-memory pipe.
    with tempfile.TemporaryDirectory(prefix="hindsight-pdf-ocr-") as scratch:
        source = Path(scratch) / "source.pdf"
        result_path = Path(scratch) / "result.json"
        source.write_bytes(file_data)
        source.chmod(0o600)
        process = multiprocessing.get_context("spawn").Process(
            target=_worker, args=(str(source), str(result_path), ocr_config, limits, deadline), daemon=True
        )
        try:
            process.start()
            while process.is_alive():
                if time.monotonic() >= deadline:
                    raise RuntimeError("PDF conversion exceeded document deadline")
                try:
                    rss = psutil.Process(process.pid).memory_info().rss
                except psutil.NoSuchProcess:
                    # psutil can see exit before multiprocessing reaps it.
                    process.join(timeout=0.05)
                    if not process.is_alive():
                        break
                    raise RuntimeError("Cannot measure PDF conversion memory") from None
                except psutil.Error:
                    raise RuntimeError("Cannot measure PDF conversion memory") from None
                if rss > limits.rss_bytes:
                    raise RuntimeError("PDF conversion exceeded sampled RSS limit")
                await asyncio.sleep(0.05)
            if time.monotonic() >= deadline:
                raise RuntimeError("PDF conversion exceeded document deadline")
            if process.exitcode != 0 or not result_path.exists():
                raise RuntimeError("PDF conversion process failed")
            # The scan output ceiling must not truncate the original text-only
            # converter. Its typed result shares the process scratch-write bound.
            if result_path.stat().st_size > limits.rendered_total_bytes:
                raise RuntimeError("PDF conversion result exceeds scratch byte limit")
            try:
                result = _RESULT_ADAPTER.validate_json(result_path.read_bytes())
            except ValidationError:
                # Malformed child transport must not expose private result values.
                raise RuntimeError("PDF conversion returned an invalid process result") from None
            if isinstance(result, _QualityResult):
                raise LowQualityOcrError(
                    "markitdown",
                    filename,
                    OcrQualityResult(False, result.reason, result.features),
                )
            if isinstance(result, _EmptyResult):
                raise NoExtractableContentError(f"No content extracted from '{filename}'")
            if isinstance(result, _ErrorResult):
                # Never propagate provider exception text, source text or secrets.
                raise RuntimeError(f"PDF conversion failed ({result.error_type})")
            if isinstance(result, _SuccessResult) and len(result.content.encode("utf-8")) > limits.output_bytes:
                raise RuntimeError("PDF conversion output exceeds byte limit")
            return result.content
        finally:
            if process.pid is not None:
                if process.is_alive():
                    process.terminate()
                process.join(timeout=0.5)
                if process.is_alive():
                    process.kill()
                    process.join(timeout=0.5)
                process.close()


def _worker(source: str, result_path: str, ocr_config: PdfOcrConfig, limits: PdfOcrLimits, deadline: float) -> None:
    # Child logging must not expose provider errors or private converted text.
    logging.disable(logging.CRITICAL)
    try:
        # Bound native CPU and scratch writes where POSIX supplies limits.
        # Memory is monitored by the parent RSS sampler on Linux and Darwin.
        try:
            import resource
        except ImportError:
            pass
        else:
            bounds = [(resource.RLIMIT_CPU, 60), (resource.RLIMIT_FSIZE, limits.rendered_total_bytes)]
            for kind, requested in bounds:
                _, hard = resource.getrlimit(kind)
                ceiling = requested if hard == resource.RLIM_INFINITY else min(requested, hard)
                resource.setrlimit(kind, (ceiling, ceiling))
        result: PdfOcrResult = _convert_pages(Path(source), ocr_config, limits, deadline)
    except LowQualityOcrError as error:
        result = _QualityResult(reason=error.reason, features=error.features)
    except NoExtractableContentError:
        result = _EmptyResult()
    except Exception as error:
        result = _ErrorResult(error_type=type(error).__name__)

    def open_result(path: str, flags: int) -> int:
        return os.open(path, flags, 0o600)

    with open(result_path, "x", encoding="utf-8", opener=open_result) as output:
        output.write(result.model_dump_json())


def _convert_pages(
    source: Path, ocr_config: PdfOcrConfig, limits: PdfOcrLimits, deadline: float
) -> _SuccessResult | _TextSuccessResult:
    import pypdfium2 as pdfium
    from markitdown import MarkItDown, StreamInfo

    from .markitdown import MarkitdownParser

    def check_deadline() -> None:
        if time.monotonic() >= deadline:
            raise RuntimeError("PDF conversion exceeded document deadline")

    def checked_text(text: str) -> str:
        if len(text.encode("utf-8")) > limits.output_bytes:
            raise RuntimeError("PDF conversion output exceeds byte limit")
        return text

    check_deadline()
    # Invalid/encrypted PDFs fail here. Never reinterpret arbitrary parse errors
    # as scanned pages. Text pages still use the original MarkItDown converter.
    with pdfium.PdfDocument(source) as pdf:
        if len(pdf) == 0:
            raise NoExtractableContentError("PDF has no pages")
        all_text = True
        for index in range(len(pdf)):
            check_deadline()
            with closing(pdf[index]) as page, closing(page.get_textpage()) as text_page:
                if not text_page.get_text_range().strip():
                    all_text = False
                    break
        text_converter = MarkItDown()
        if all_text:
            # Enabling image OCR must not impose scan input/page/output limits
            # on the pre-existing whole-document text conversion. Native work
            # still stays inside this cancellable, resource-monitored process.
            result = text_converter.convert(str(source))
            content = result.text_content or ""
            check_deadline()
            if content.strip():
                return _TextSuccessResult(content=content)
            raise NoExtractableContentError("PDF has no extractable content")

        if source.stat().st_size > limits.input_bytes:
            raise RuntimeError("PDF input exceeds byte limit")
        if len(pdf) > limits.pages:
            raise RuntimeError("PDF exceeds page limit")

        pieces: list[str] = []
        total_pixels = rendered_bytes = 0
        ocr_client: OpenAI | None = None
        try:
            for index in range(len(pdf)):
                check_deadline()
                with pdfium.PdfDocument.new() as single:
                    single.import_pages(pdf, [index])
                    data = _BoundedBuffer(limits.input_bytes)
                    single.save(data)
                    data.seek(0)
                    # Per-page text conversion preserves form extraction and
                    # pdfminer's text handling. Failure stays an operational error.
                    result = text_converter.convert_stream(data, stream_info=StreamInfo(extension=".pdf"))
                    text = checked_text(result.text_content or "")
                if text.strip():
                    pieces.append(text.strip())
                else:
                    with closing(pdf[index]) as page:
                        width, height = page.get_size()
                        if not all(math.isfinite(x) and x > 0 for x in (width, height)):
                            raise RuntimeError("PDF page has invalid dimensions")
                        pixels = math.ceil(width * limits.scale) * math.ceil(height * limits.scale)
                        total_pixels += pixels
                        if pixels > limits.page_pixels or total_pixels > limits.total_pixels:
                            raise RuntimeError("PDF render exceeds pixel limit")
                        with closing(page.render(scale=limits.scale)) as bitmap:
                            with bitmap.to_pil() as image:
                                # Only provably blank white pages may be skipped.
                                if all(extrema == (255, 255) for extrema in image.getextrema()):
                                    continue
                                png = _BoundedBuffer(limits.rendered_page_bytes)
                                image.save(png, format="PNG")
                        rendered_bytes += png.tell()
                        if rendered_bytes > limits.rendered_total_bytes:
                            raise RuntimeError("PDF render exceeds total byte limit")
                        check_deadline()
                        if ocr_client is None:
                            options = MarkitdownParser(
                                ocr_enabled=True,
                                ocr_api_key=ocr_config.api_key,
                                ocr_base_url=ocr_config.base_url,
                                ocr_model=ocr_config.model,
                                ocr_prompt=ocr_config.prompt,
                                ocr_default_headers=ocr_config.default_headers,
                            )._build_ocr_options(
                                api_key=ocr_config.api_key,
                                base_url=ocr_config.base_url,
                                model=ocr_config.model,
                                prompt=ocr_config.prompt,
                                default_headers=ocr_config.default_headers,
                            )
                            # MarkItDown's inherited image-converter interface
                            # requires the synchronous SDK. Keep that compatibility
                            # inside this disposable process, never on the API loop.
                            ocr_client = cast("OpenAI", options.llm_client)
                            ocr_model, ocr_prompt = options.llm_model, options.llm_prompt
                        image_converter = MarkItDown(
                            llm_client=ocr_client.with_options(
                                timeout=min(limits.request_seconds, deadline - time.monotonic()), max_retries=0
                            ),
                            llm_model=ocr_model,
                            llm_prompt=ocr_prompt,
                        )
                        png.seek(0)
                        result = image_converter.convert_stream(
                            png, stream_info=StreamInfo(extension=".png", mimetype="image/png")
                        )
                        text = checked_text(result.text_content or "")
                        if not text.strip():
                            raise NoExtractableContentError("PDF page has no extractable content")
                        quality = evaluate_ocr_quality(text)
                        if not quality.accepted:
                            raise LowQualityOcrError("markitdown", "source.pdf", quality)
                        pieces.append(text.strip())
                checked_text("\n\n".join(pieces))
            check_deadline()
            content = checked_text("\n\n".join(pieces))
            if not content.strip():
                raise NoExtractableContentError("PDF has no extractable content")
            return _SuccessResult(content=content)
        finally:
            if ocr_client is not None:
                ocr_client.close()
