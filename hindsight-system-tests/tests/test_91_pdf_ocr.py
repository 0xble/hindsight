"""A scanned PDF stays attached to its original upload and retains atomically.

The API and worker are real. OCR and extraction use the existing HTTP stub.
No SQL or engine imports are used. Conversion and downstream retain are checked
as separate operations, with original bytes retrieved through the public API.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
import uuid
import zlib
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
from hindsight_client import Hindsight

from hindsight_system_tests import start_hindsight_server, wait_until_settled
from hindsight_system_tests.payloads import Consolidation, Observation, extracted, fact
from hindsight_system_tests.rulebook import ChatRequest, ChatRule, StubbedReply

pytestmark = pytest.mark.asyncio

OCR_PROMPT = "Transcribe the scanned source verbatim for the PDF system story."
SOURCE_TEXT = "Alice completed the canonical source review on 2020-03-04."
OBSERVATION = "Alice completed the canonical source review."


def scanned_pdf() -> bytes:
    """Generate two image-only pages with distinct visible raster patterns.

    Standard-library pixels, Flate image streams and explicit PDF objects keep
    this fixture reviewable without a binary addition or image-library test
    dependency. The fake provider supplies semantic text, so this exercises the
    real scan mechanics, not model fidelity on the synthetic page patterns.
    """
    width, height = 240, 160
    objects = [b"<< /Type /Catalog /Pages 2 0 R >>", b"<< /Type /Pages /Kids [3 0 R 6 0 R] /Count 2 >>"]
    for index in range(2):
        page_id = 3 + index * 3
        pixels = bytearray([255] * (width * height * 3))
        # One versus two black bars distinguish page identities after rendering.
        for bar in range(index + 1):
            for y in range(15, 65):
                for x in range(15 + bar * 30, 25 + bar * 30):
                    start = (y * width + x) * 3
                    pixels[start : start + 3] = b"\x00\x00\x00"
        image = zlib.compress(bytes(pixels))
        drawing = b"q 240 0 0 160 0 0 cm /Scan Do Q"
        objects.extend(
            [
                (
                    f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {width} {height}] "
                    f"/Resources << /XObject << /Scan {page_id + 2} 0 R >> >> /Contents {page_id + 1} 0 R >>"
                ).encode(),
                b"<< /Length " + str(len(drawing)).encode() + b" >>\nstream\n" + drawing + b"\nendstream",
                (
                    f"<< /Type /XObject /Subtype /Image /Width {width} /Height {height} "
                    f"/ColorSpace /DeviceRGB /BitsPerComponent 8 /Filter /FlateDecode /Length {len(image)} >>\nstream\n"
                ).encode()
                + image
                + b"\nendstream",
            ]
        )
    output = bytearray(b"%PDF-1.4\n")
    offsets: list[int] = []
    for index, body in enumerate(objects, 1):
        offsets.append(len(output))
        output.extend(f"{index} 0 obj\n".encode() + body + b"\nendobj\n")
    xref = len(output)
    output.extend(f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode())
    for offset in offsets:
        output.extend(f"{offset:010d} 00000 n \n".encode())
    output.extend(f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode())
    return bytes(output)


@pytest.fixture
def source(tmp_path: Path) -> Path:
    path = tmp_path / "scanned-two-pages.pdf"
    path.write_bytes(scanned_pdf())
    return path


@pytest.fixture(scope="session")
def pdf_server(stub_server, tmp_path_factory) -> Iterator[object]:
    server = start_hindsight_server(
        stub_url=stub_server.url,
        log_path=tmp_path_factory.mktemp("pdf-ocr-server") / "server.log",
        extra_env={
            "HINDSIGHT_API_FILE_PARSER_MARKITDOWN_OCR_ENABLED": "true",
            "HINDSIGHT_API_FILE_PARSER_MARKITDOWN_OCR_API_KEY": "stub-key",
            "HINDSIGHT_API_FILE_PARSER_MARKITDOWN_OCR_BASE_URL": f"{stub_server.url}/v1",
            "HINDSIGHT_API_FILE_PARSER_MARKITDOWN_OCR_MODEL": "stub-ocr",
            "HINDSIGHT_API_FILE_PARSER_MARKITDOWN_OCR_PROMPT": OCR_PROMPT,
            "HINDSIGHT_API_FILE_DELETE_AFTER_RETAIN": "false",
            "HINDSIGHT_API_ENABLE_DOCUMENT_EXPORT_API": "true",
        },
    )
    yield server
    server.stop()


@pytest.fixture
async def pdf_client(pdf_server) -> AsyncIterator[Hindsight]:
    client = Hindsight(base_url=pdf_server.url)
    yield client
    await client.aclose()


@pytest.fixture
async def pdf_bank(pdf_client) -> AsyncIterator[str]:
    bank = f"systest-pdf-{uuid.uuid4().hex[:12]}"
    await pdf_client.acreate_bank(bank_id=bank)
    await pdf_client.banks.update_bank_config(
        bank,
        {
            "updates": {
                "store_document_text": True,
            }
        },
    )
    yield bank
    await pdf_client.banks.delete_bank(bank)


async def upload(client, bank, source: Path):
    return await client.aretain_files(
        bank_id=bank,
        files=[source],
        files_metadata=[
            {
                "document_id": "original-source",
                "parser": ["markitdown"],
                "context": "Canonical source authored by Alice",
                "metadata": {"source": "canonical"},
                "tags": ["original-source"],
                "timestamp": "2020-03-04T12:00:00Z",
            }
        ],
    )


async def original_bytes(client, key):
    response = await client.document_transfer.download_file_without_preload_content(key)
    return bytes(await response.read())


async def test_scanned_pdf_retains_consolidates_and_original_provenance_reads_back(pdf_client, pdf_bank, llm, source):
    llm.on_chat(contains=OCR_PROMPT).returns_text(SOURCE_TEXT)
    llm.on_step("extract_facts", contains="canonical source review").returns(
        extracted(
            fact(
                SOURCE_TEXT,
                when="2020-03-04",
                who="Alice",
                entities=["Alice"],
                fact_kind="event",
                occurred_start="2020-03-04T12:00:00Z",
                occurred_end="2020-03-04T12:00:00Z",
            )
        )
    )

    def observe_source(request: ChatRequest) -> Consolidation:
        # The generic helper also sees illustrative UUIDs in the system prompt.
        # Cite only the newly retained source line, never prompt examples.
        ids = set(re.findall(r"\[([0-9a-f-]{36})\]\s+" + re.escape(SOURCE_TEXT.rstrip(".")), request.all_text))
        assert len(ids) == 1
        return Consolidation(creates=[Observation(text=OBSERVATION, source_fact_ids=list(ids))])

    llm.on_step("consolidate").answers_with(observe_source)
    submission = await upload(pdf_client, pdf_bank, source)
    operation_id = submission.operation_ids[0]
    await wait_until_settled(pdf_client, pdf_bank)
    conversion = await pdf_client.operations.get_operation_status(pdf_bank, operation_id, include_payload=True)
    assert conversion.status == "completed"
    assert conversion.operation_type == "file_convert_retain"
    original = conversion.task_payload
    assert original["document_id"] == "original-source"
    assert original["original_filename"] == source.name
    assert original["content_type"] == "application/pdf"
    assert original["parser"] == ["markitdown"]
    listing = await pdf_client.operations.list_operations(pdf_bank, limit=100)
    retained = [op for op in listing.operations if op.task_type == "retain"]
    assert len(retained) == 1 and retained[0].status == "completed"
    retain = await pdf_client.operations.get_operation_status(pdf_bank, retained[0].id, include_payload=True)
    payload = retain.task_payload
    assert payload["_file_metadata"] == {
        "file_storage_key": original["storage_key"],
        "file_original_name": source.name,
        "file_content_type": "application/pdf",
    }
    item = payload["contents"][0]
    assert item["document_id"] == original["document_id"]
    assert item["context"] == original["context"]
    assert item["metadata"] == original["metadata"]
    assert item["tags"] == original["tags"]
    assert item["event_date"] == original["timestamp"]
    assert (
        hashlib.sha256(await original_bytes(pdf_client, original["storage_key"])).digest()
        == hashlib.sha256(source.read_bytes()).digest()
    )
    document = await pdf_client.documents.get_document(pdf_bank, "original-source")
    assert SOURCE_TEXT in document.original_text
    memories = await pdf_client.memory.list_memories(pdf_bank, limit=100)
    raw = [memory for memory in memories.items if memory.document_id == "original-source"]
    assert len(raw) == 1
    assert raw[0].context == original["context"]
    assert raw[0].metadata == original["metadata"]
    assert raw[0].occurred_start.startswith("2020-03-04")
    # Ordinary background work runs without disabling observation/consolidation.
    # The observation's evidence must resolve to the retained original-source fact.
    observation = next(memory for memory in memories.items if memory.text == OBSERVATION)
    assert observation.source_memory_ids == [raw[0].id]
    assert any(
        operation.task_type == "consolidation" and operation.status == "completed" for operation in listing.operations
    )
    assert len([call for call in llm.calls if OCR_PROMPT in call.all_text]) == 2


async def test_bad_second_page_creates_no_document_or_retain_and_keeps_original(pdf_client, pdf_bank, llm, source):
    pages = []

    def page_reply(request):
        pages.append(request)
        text = SOURCE_TEXT if len(pages) == 1 else "No readable text in this image."
        return StubbedReply(message={"role": "assistant", "content": text}, finish_reason="stop")

    llm.add_rule(ChatRule(contains=(OCR_PROMPT,), respond=page_reply))
    submission = await upload(pdf_client, pdf_bank, source)
    async with asyncio.timeout(30):
        while True:
            conversion = await pdf_client.operations.get_operation_status(
                pdf_bank, submission.operation_ids[0], include_payload=True
            )
            if conversion.status == "failed":
                break
            await asyncio.sleep(0.1)
    assert len(pages) == 2
    assert conversion.details.actual_instance.failure_class == "low_quality_ocr"
    listing = await pdf_client.operations.list_operations(pdf_bank, limit=100)
    assert all(op.task_type != "retain" for op in listing.operations)
    assert (await pdf_client.documents.list_documents(pdf_bank)).total == 0
    assert (await pdf_client.memory.list_memories(pdf_bank)).total == 0
    restored = await original_bytes(pdf_client, conversion.task_payload["storage_key"])
    assert hashlib.sha256(restored).digest() == hashlib.sha256(source.read_bytes()).digest()
