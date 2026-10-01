"""Real PDF parsing and HTTP OCR plumbing with a local deterministic provider."""

import asyncio
import io
import json
import subprocess
import sys
import threading
import time
from contextlib import closing, contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from PIL import Image, ImageDraw

from hindsight_api.engine.parsers import FileParserRegistry
from hindsight_api.engine.parsers.markitdown import MarkitdownParser
from hindsight_api.engine.parsers.ocr_quality import LowQualityOcrError
from tests.pdf_ocr_fixtures import SYNTHETIC_PASSWORD, encrypted_pdf


def test_pdf_worker_import_does_not_load_application_or_local_ml():
    # Spawn unpickles the worker target by importing this qualified module in
    # a fresh interpreter. Parent pytest/engine imports must not mask its cost.
    subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; import hindsight_api.engine.parsers.pdf_ocr; "
            "assert 'hindsight_api.engine.memory_engine' not in sys.modules; "
            "assert 'hindsight_api.engine.llm_wrapper' not in sys.modules; "
            "assert 'torch' not in sys.modules; "
            "assert 'sentence_transformers' not in sys.modules",
        ],
        check=True,
        capture_output=True,
        text=True,
    )


def test_engine_public_exports_preserve_original_objects():
    import importlib

    import hindsight_api.engine as engine

    exports = {
        "cross_encoder": ["CrossEncoderModel", "LocalSTCrossEncoder", "RemoteTEICrossEncoder"],
        "db_utils": ["acquire_with_retry"],
        "embeddings": ["Embeddings", "LocalSTEmbeddings", "RemoteTEIEmbeddings"],
        "llm_wrapper": ["LLMConfig"],
        "memory_engine": [
            "MemoryEngine",
            "UnqualifiedTableError",
            "fq_table",
            "get_current_schema",
            "validate_sql_schema",
        ],
        "response_models": ["MemoryFact", "RecallResult", "ReflectResult"],
        "search.trace": [
            "EntryPoint",
            "NodeVisit",
            "QueryInfo",
            "SearchPhaseMetrics",
            "SearchSummary",
            "SearchTrace",
            "WeightComponents",
        ],
        "search.tracer": ["SearchTracer"],
    }
    expected = {name for names in exports.values() for name in names}
    assert set(engine.__all__) == expected
    assert expected <= set(dir(engine))
    for module, names in exports.items():
        original = importlib.import_module(f"hindsight_api.engine.{module}")
        for name in names:
            assert getattr(engine, name) is getattr(original, name)
            assert engine.__dict__[name] is getattr(original, name)
    with pytest.raises(AttributeError):
        engine.missing_public_export


def scanned_pdf(pages=1, *, size=(240, 160)):
    images = []
    for page in range(pages):
        image = Image.new("RGB", size, "white")
        ImageDraw.Draw(image).text((15, 15), f"Page {page + 1}: Alice completed the review.", fill="black")
        images.append(image)
    output = io.BytesIO()
    images[0].save(output, format="PDF", save_all=True, append_images=images[1:])
    for image in images:
        image.close()
    return output.getvalue()


@contextmanager
def ocr_server(responses):
    calls = []
    scripted = iter(responses)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            calls.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            response = next(scripted)
            if isinstance(response, float):
                time.sleep(response)
                response = "Alice completed the review."
            status = 500 if response is None else 200
            payload = {"choices": [{"message": {"role": "assistant", "content": response}}]}
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            try:
                self.wfile.write(json.dumps(payload).encode())
            except BrokenPipeError:
                pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1", calls
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def parser(url):
    return MarkitdownParser(
        ocr_enabled=True,
        ocr_api_key="synthetic-key",
        ocr_base_url=url,
        ocr_model="existing-ocr-model",
        ocr_prompt="Transcribe the source verbatim.",
    )


@pytest.mark.asyncio
async def test_image_only_pages_use_existing_ocr_route_in_order():
    with ocr_server(["Alice completed the review on Monday.", "Bob approved the final changes on Tuesday."]) as (
        url,
        calls,
    ):
        result = await parser(url).convert(scanned_pdf(2), "original.pdf")
    assert result.index("Alice") < result.index("Bob")
    assert len(calls) == 2
    assert all(call["model"] == "existing-ocr-model" for call in calls)
    assert all(call["messages"][0]["content"][0]["text"] == "Transcribe the source verbatim." for call in calls)
    assert all(
        call["messages"][0]["content"][1]["image_url"]["url"].startswith("data:image/png;base64,") for call in calls
    )


@pytest.mark.asyncio
async def test_a_bad_page_rejects_whole_document_including_after_good_page():
    with ocr_server(["Alice completed the review on Monday.", "No readable text in this image."]) as (url, calls):
        with pytest.raises(LowQualityOcrError):
            await parser(url).convert(scanned_pdf(2), "original.pdf")
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_provider_failure_is_not_a_partial_success():
    with ocr_server(["Alice completed the review on Monday.", None]) as (url, calls):
        with pytest.raises(RuntimeError):
            await parser(url).convert(scanned_pdf(2), "original.pdf")
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_disabled_ocr_and_corrupt_pdf_never_call_provider():
    import pypdfium2 as pdfium

    encrypted = encrypted_pdf()
    # Prove the fixture is valid encrypted content, not a corrupt PDF that
    # would make the no-provider assertion vacuously pass.
    with pdfium.PdfDocument(encrypted, password=SYNTHETIC_PASSWORD) as pdf:
        with closing(pdf[0]) as page, closing(page.get_textpage()) as text:
            assert "Alice completed the review." in text.get_text_range()

    with ocr_server([]) as (url, calls):
        with pytest.raises(RuntimeError):
            await MarkitdownParser().convert(scanned_pdf(), "original.pdf")
        with pytest.raises(RuntimeError):
            await parser(url).convert(b"%PDF-1.4 corrupt source", "corrupt.pdf")
        with pytest.raises(RuntimeError):
            await parser(url).convert(encrypted, "encrypted.pdf")
    assert calls == []


@pytest.mark.asyncio
async def test_rejected_pdf_still_uses_existing_fallback_chain():
    from hindsight_api.engine.parsers.base import FileParser

    class Fallback(FileParser):
        async def convert(self, file_data, filename):
            return "Fallback extracted the complete original source."

        def name(self):
            return "fallback"

    registry = FileParserRegistry()
    with ocr_server(["No readable text in this image."]) as (url, calls):
        registry.register(parser(url))
        registry.register(Fallback())
        result = await registry.convert_with_fallback(
            ["markitdown", "fallback"], scanned_pdf(), "original.pdf", "application/pdf"
        )
    assert result.parser_name == "fallback"
    assert len(calls) == 1


def text_pdf(text="Original text page from Alice.", *, padding_bytes=0, unicode_mapping=None):
    stream = f"BT /F1 12 Tf 15 80 Td ({text}) Tj ET".encode()
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 240 160] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
    ]
    if unicode_mapping is not None:
        # A transparent synthetic font maps each A glyph to a Unicode sequence.
        # This gives a real large extraction without allocating a million glyphs.
        mapping = unicode_mapping.encode("utf-16-be").hex().encode()
        cmap = (
            b"/CIDInit /ProcSet findresource begin 12 dict begin begincmap\n"
            b"/CIDSystemInfo << /Registry (Adobe) /Ordering (UCS) /Supplement 0 >> def\n"
            b"/CMapName /SyntheticUnicode def /CMapType 2 def\n"
            b"1 begincodespacerange <00> <FF> endcodespacerange\n"
            b"1 beginbfchar <41> <" + mapping + b"> endbfchar\n"
            b"endcmap CMapName currentdict /CMap defineresource pop end end"
        )
        objects[3] = b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /ToUnicode 6 0 R >>"
        objects.append(b"<< /Length " + str(len(cmap)).encode() + b" >>\nstream\n" + cmap + b"\nendstream")
    # Valid padding comment, inside the PDF and included in the xref offsets.
    data = b"%PDF-1.4\n" + (b"%" + b"x" * padding_bytes + b"\n" if padding_bytes else b"")
    offsets = []
    for index, body in enumerate(objects, 1):
        offsets.append(len(data))
        data += f"{index} 0 obj\n".encode() + body + b"\nendobj\n"
    offset = len(data)
    count = len(objects) + 1
    data += f"xref\n0 {count}\n0000000000 65535 f \n".encode()
    data += b"".join(f"{pos:010d} 00000 n \n".encode() for pos in offsets)
    return data + f"trailer\n<< /Size {count} /Root 1 0 R >>\nstartxref\n{offset}\n%%EOF\n".encode()


def joined_pdf(sources):
    import pypdfium2 as pdfium

    with pdfium.PdfDocument.new() as joined:
        for data in sources:
            with pdfium.PdfDocument(data) as source:
                joined.import_pages(source)
        output = io.BytesIO()
        joined.save(output)
    return output.getvalue()


@pytest.mark.asyncio
async def test_text_only_pdf_preserves_existing_conversion_without_ocr_calls():
    original = text_pdf()
    baseline = await MarkitdownParser().convert(original, "source.pdf")
    with ocr_server([]) as (url, calls):
        result = await parser(url).convert(original, "source.pdf")
    assert result == baseline
    assert calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["pages", "input", "output"])
async def test_text_only_pdf_above_ocr_bounds_preserves_real_whole_document_conversion(case):
    from hindsight_api.engine.parsers.pdf_ocr import PDF_OCR_LIMITS

    if case == "pages":
        original = joined_pdf([text_pdf(f"Original page {page}.") for page in range(PDF_OCR_LIMITS.pages + 1)])
    elif case == "input":
        original = text_pdf(padding_bytes=PDF_OCR_LIMITS.input_bytes)
        assert len(original) > PDF_OCR_LIMITS.input_bytes
    else:
        original = text_pdf("A" * 8193, unicode_mapping="\U0001f600" * 32)
    baseline = await MarkitdownParser().convert(original, "source.pdf")
    assert baseline.strip()
    if case == "output":
        assert len(baseline.encode("utf-8")) > PDF_OCR_LIMITS.output_bytes
    with ocr_server([]) as (url, calls):
        result = await parser(url).convert(original, "source.pdf")
    assert result == baseline
    assert calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("bound", ["input_bytes", "pages", "output_bytes"])
async def test_mixed_pdf_still_enforces_scan_bounds(monkeypatch, bound):
    from dataclasses import replace

    from hindsight_api.engine.parsers import pdf_ocr

    original = joined_pdf([text_pdf(), scanned_pdf()])
    monkeypatch.setattr(pdf_ocr, "PDF_OCR_LIMITS", replace(pdf_ocr.PDF_OCR_LIMITS, **{bound: 1}))
    with ocr_server([]) as (url, calls):
        with pytest.raises(RuntimeError):
            await parser(url).convert(original, "source.pdf")
    assert calls == []


@pytest.mark.asyncio
async def test_mixed_text_and_scanned_pages_preserve_order():
    import pypdfium2 as pdfium

    with pdfium.PdfDocument.new() as mixed:
        for data in [text_pdf("First original text page."), scanned_pdf(), text_pdf("Last original text page.")]:
            with pdfium.PdfDocument(data) as source:
                mixed.import_pages(source)
        output = io.BytesIO()
        mixed.save(output)
    with ocr_server(["Middle scanned page approved by Alice."]) as (url, calls):
        result = await parser(url).convert(output.getvalue(), "source.pdf")
    assert result.index("First") < result.index("Middle") < result.index("Last")
    assert len(calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bound,value",
    [
        ("input_bytes", 1),
        ("pages", 1),
        ("page_pixels", 1),
        ("total_pixels", 1),
        ("rendered_page_bytes", 1),
        ("rendered_total_bytes", 1),
    ],
)
async def test_limits_reject_before_ocr_allocation_or_call(monkeypatch, bound, value):
    from dataclasses import replace

    from hindsight_api.engine.parsers import pdf_ocr

    monkeypatch.setattr(pdf_ocr, "PDF_OCR_LIMITS", replace(pdf_ocr.PDF_OCR_LIMITS, **{bound: value}))
    with ocr_server([]) as (url, calls):
        with pytest.raises(RuntimeError):
            await parser(url).convert(scanned_pdf(2), "source.pdf")
    assert calls == []


@pytest.mark.asyncio
async def test_document_deadline_kills_real_process_and_cleans_scratch(monkeypatch, tmp_path):
    import multiprocessing
    import tempfile
    from dataclasses import replace

    from hindsight_api.engine.parsers import pdf_ocr

    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    # A parallel gate can spend several seconds starting the spawned interpreter.
    # Keep the real document deadline shorter than the hung provider request,
    # with enough startup allowance to exercise termination during that request.
    monkeypatch.setattr(pdf_ocr, "PDF_OCR_LIMITS", replace(pdf_ocr.PDF_OCR_LIMITS, seconds=20.0, request_seconds=45.0))
    before = {child.pid for child in multiprocessing.active_children()}
    with ocr_server([40.0]) as (url, calls):
        with pytest.raises(RuntimeError, match="deadline"):
            await parser(url).convert(scanned_pdf(), "source.pdf")
    assert calls, "the real OCR request must be active when the deadline fires"
    assert {child.pid for child in multiprocessing.active_children()} == before
    assert list(tmp_path.glob("hindsight-pdf-ocr-*")) == []


@pytest.mark.asyncio
async def test_cancellation_kills_real_process_and_cleans_scratch(monkeypatch, tmp_path):
    import multiprocessing
    import tempfile

    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    before = {child.pid for child in multiprocessing.active_children()}
    with ocr_server([10.0]) as (url, calls):
        task = asyncio.create_task(parser(url).convert(scanned_pdf(), "source.pdf"))
        async with asyncio.timeout(20):
            while not calls:
                await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert {child.pid for child in multiprocessing.active_children()} == before
    assert list(tmp_path.glob("hindsight-pdf-ocr-*")) == []


def allocating_worker(source, result_path, ocr_config, limits, deadline):
    allocation = bytearray(256 * 1024 * 1024)
    allocation[-1] = 1
    time.sleep(10)


def malformed_result_worker(source, result_path, ocr_config, limits, deadline):
    from pathlib import Path

    Path(result_path).write_text('{"kind":"ok","content":{"private":"must not appear in errors"}}')


@pytest.mark.asyncio
async def test_process_result_boundary_rejects_malformed_values_without_exposing_content(monkeypatch, tmp_path):
    import tempfile

    from hindsight_api.engine.parsers import pdf_ocr

    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    monkeypatch.setattr(pdf_ocr, "_worker", malformed_result_worker)
    with pytest.raises(RuntimeError, match="invalid process result") as error:
        await parser("http://127.0.0.1:1/v1").convert(scanned_pdf(), "source.pdf")
    assert "private" not in str(error.value)
    assert "must not appear" not in str(error.value)
    assert list(tmp_path.glob("hindsight-pdf-ocr-*")) == []


@pytest.mark.asyncio
async def test_sampled_rss_limit_kills_actual_allocating_child(monkeypatch, tmp_path):
    import tempfile
    from dataclasses import replace

    import psutil

    from hindsight_api.engine.parsers import pdf_ocr

    seen = []
    read_memory = psutil.Process.memory_info

    def memory_info(process):
        info = read_memory(process)
        seen.append((process.pid, info.rss))
        return info

    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    monkeypatch.setattr(pdf_ocr, "_worker", allocating_worker)
    monkeypatch.setattr(psutil.Process, "memory_info", memory_info)
    monkeypatch.setattr(pdf_ocr, "PDF_OCR_LIMITS", replace(pdf_ocr.PDF_OCR_LIMITS, rss_bytes=128 * 1024 * 1024))
    with pytest.raises(RuntimeError, match="sampled RSS"):
        await parser("http://127.0.0.1:1/v1").convert(scanned_pdf(), "source.pdf")
    assert max(rss for _, rss in seen) > 128 * 1024 * 1024
    assert all(not psutil.pid_exists(pid) for pid, _ in seen)
    assert list(tmp_path.glob("hindsight-pdf-ocr-*")) == []


@pytest.mark.asyncio
async def test_unsupported_memory_measurement_fails_closed(monkeypatch, tmp_path):
    import tempfile

    import psutil

    def denied(process):
        raise psutil.AccessDenied(process.pid)

    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    monkeypatch.setattr(psutil.Process, "memory_info", denied)
    with pytest.raises(RuntimeError, match="Cannot measure"):
        await parser("http://127.0.0.1:1/v1").convert(scanned_pdf(), "source.pdf")
    assert list(tmp_path.glob("hindsight-pdf-ocr-*")) == []


@pytest.mark.asyncio
async def test_request_timeout_is_bounded_without_provider_retries(monkeypatch):
    from dataclasses import replace

    from hindsight_api.engine.parsers import pdf_ocr

    monkeypatch.setattr(pdf_ocr, "PDF_OCR_LIMITS", replace(pdf_ocr.PDF_OCR_LIMITS, request_seconds=0.15))
    with ocr_server([5.0]) as (url, calls):
        with pytest.raises(RuntimeError):
            await parser(url).convert(scanned_pdf(), "source.pdf")
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_output_bound_rejects_complete_oversized_provider_reply(monkeypatch):
    from dataclasses import replace

    from hindsight_api.engine.parsers import pdf_ocr

    monkeypatch.setattr(pdf_ocr, "PDF_OCR_LIMITS", replace(pdf_ocr.PDF_OCR_LIMITS, output_bytes=256))
    with ocr_server(["Alice completed the source review. " * 100]) as (url, calls):
        with pytest.raises(RuntimeError):
            await parser(url).convert(scanned_pdf(), "source.pdf")
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_sparse_evidence_is_admitted_and_provably_blank_pages_need_no_ocr():
    from hindsight_api.engine.parsers.base import NoExtractableContentError

    with ocr_server(["Alice"]) as (url, calls):
        assert "Alice" in await parser(url).convert(scanned_pdf(), "source.pdf")
    assert len(calls) == 1
    with Image.new("RGB", (240, 160), "white") as image:
        blank = io.BytesIO()
        image.save(blank, format="PDF")
    with ocr_server([]) as (url, calls):
        with pytest.raises(NoExtractableContentError):
            await parser(url).convert(blank.getvalue(), "source.pdf")
    assert calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "page_response,succeeds",
    [("Alice completed the source review on Monday.", True), ("No readable text in this image.", False)],
)
async def test_real_conversion_handler_preserves_provenance_and_queues_only_complete_pdf(
    monkeypatch, page_response, succeeds
):
    import copy
    import hashlib
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from hindsight_api.engine.memory_engine import MemoryEngine

    class Backend:
        _wraps_backend = True
        execute = AsyncMock()

        def acquire(self):
            return self

        def transaction(self):
            return self

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

    original = scanned_pdf(2)
    digest = hashlib.sha256(original).hexdigest()
    payload = {
        "bank_id": "synthetic-pdf-bank",
        "storage_key": "banks/synthetic-pdf-bank/original-key",
        "document_id": "original-doc",
        "operation_id": "11111111-1111-1111-1111-111111111111",
        "original_filename": "original.pdf",
        "content_type": "application/pdf",
        "parser": ["markitdown"],
        "context": "Original source authored by Alice",
        "metadata": {"source": "canonical"},
        "tags": ["source-tag"],
        "document_tags": ["document-tag"],
        "timestamp": "2020-03-04T12:00:00Z",
        "strategy": "original-strategy",
        "_tenant_id": "synthetic-tenant",
        "_api_key_id": "synthetic-key-id",
    }
    baseline = copy.deepcopy(payload)
    backend = Backend()
    storage = SimpleNamespace(retrieve=AsyncMock(return_value=original), delete=AsyncMock())
    task_backend = SimpleNamespace(submit_task=AsyncMock())
    registry = FileParserRegistry()
    engine = SimpleNamespace(
        _file_storage=storage,
        _parser_registry=registry,
        _operation_validator=None,
        _get_backend=AsyncMock(return_value=backend),
        _task_backend=task_backend,
    )
    monkeypatch.setattr("hindsight_api.config.get_config", lambda: SimpleNamespace(file_delete_after_retain=False))
    with ocr_server(["First scanned source page approved by Bob.", page_response]) as (url, calls):
        registry.register(parser(url))
        if succeeds:
            await MemoryEngine._handle_file_convert_retain(engine, payload)
        else:
            with pytest.raises(RuntimeError):
                await MemoryEngine._handle_file_convert_retain(engine, payload)
    assert len(calls) == 2
    assert payload == baseline
    assert hashlib.sha256(original).hexdigest() == digest
    storage.retrieve.assert_awaited_once_with(payload["storage_key"])
    storage.delete.assert_not_awaited()
    if not succeeds:
        backend.execute.assert_not_awaited()
        task_backend.submit_task.assert_not_awaited()
        return
    queued = task_backend.submit_task.await_args.args[0]
    item = queued["contents"][0]
    assert item["document_id"] == payload["document_id"]
    assert item["context"] == payload["context"]
    assert item["metadata"] == payload["metadata"]
    assert item["tags"] == payload["tags"]
    assert item["event_date"] == payload["timestamp"]
    assert queued["document_tags"] == payload["document_tags"]
    assert queued["strategy"] == payload["strategy"]
    assert queued["_tenant_id"] == payload["_tenant_id"]
    assert queued["_api_key_id"] == payload["_api_key_id"]
    assert queued["_file_metadata"] == {
        "file_storage_key": payload["storage_key"],
        "file_original_name": payload["original_filename"],
        "file_content_type": payload["content_type"],
    }
    assert backend.execute.await_args_list[0].args[5] == "pending", "retain is pending when conversion completes"
