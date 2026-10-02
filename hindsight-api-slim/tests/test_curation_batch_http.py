"""DB-free HTTP contract regressions for raw-curation-v2 batch identifiers."""

from collections.abc import AsyncIterator
from unittest.mock import MagicMock

import httpx
import pytest
import pytest_asyncio
from pydantic import TypeAdapter, ValidationError

from hindsight_api.api import create_app
from hindsight_api.engine.curation_batch import (
    BatchId,
    CurationApplyRequest,
    CurationBatchConflict,
    CurationChange,
    CurationInventory,
    CurationPreview,
)
from hindsight_api.engine.memory_engine import MemoryEngine

ROOT = "/v1/default/banks/test-curation-http/curation-batches"
MEMORY_ID = "00000000-0000-0000-0000-000000000001"
REVISION = "0" * 64


@pytest.fixture
def batch_engine() -> MagicMock:
    # Stub only the engine boundary; route matching and Pydantic validation are real.
    engine = MagicMock(spec=MemoryEngine)
    engine.audit_logger = None
    engine.resolve_bank_alias.return_value = "test-curation-http"
    engine.get_curation_batch.return_value = None
    engine.revert_curation_batch.return_value = None
    engine.apply_curation_batch.side_effect = CurationBatchConflict("test manifest conflict")
    engine.preview_curation_batch.return_value = CurationPreview(
        closure_revision=REVISION,
        targets=[],
        inventory=CurationInventory(
            targets=0, observations=0, peers=0, entities=0, links=0, history_rows=0, snapshot_bytes=0, source_bytes=0
        ),
    )
    return engine


@pytest_asyncio.fixture
async def batch_client(batch_engine: MagicMock) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(batch_engine, initialize_memory=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        yield client


def apply_request() -> CurationApplyRequest:
    return CurationApplyRequest(
        protocol="raw-curation-v2",
        expected_closure_revision=REVISION,
        changes=[
            CurationChange(
                memory_id=MEMORY_ID,
                memory_revision=REVISION,
                source_revision=REVISION,
                action="invalidate",
                reason="test correction",
            )
        ],
    )


@pytest.mark.asyncio
async def test_clear_memories_maps_active_capsule_conflict_to_409(
    batch_client: httpx.AsyncClient, batch_engine: MagicMock
) -> None:
    batch_engine.delete_bank.side_effect = CurationBatchConflict("Active curation capsules prevent bank deletion")
    response = await batch_client.delete("/v1/default/banks/test-curation-http/memories")
    assert response.status_code == 409, response.text
    assert response.json() == {"detail": "Active curation capsules prevent bank deletion"}


def test_preview_is_not_a_batch_id() -> None:
    with pytest.raises(ValidationError, match="reserved"):
        TypeAdapter(BatchId).validate_python("preview")


@pytest.mark.asyncio
@pytest.mark.parametrize("method,suffix", [("GET", ""), ("POST", "/revert")])
async def test_reserved_preview_batch_id_is_rejected_over_http(
    batch_client: httpx.AsyncClient, batch_engine: MagicMock, method: str, suffix: str
) -> None:
    response = await batch_client.request(
        method,
        ROOT + "/preview" + suffix,
        **({"json": {"protocol": "raw-curation-v2", "expected_receipt_revision": REVISION}} if suffix else {}),
    )
    assert response.status_code == 422, response.text
    errors = response.json()["detail"]
    assert any(error["loc"] == ["path", "batch_id"] and "reserved" in error["msg"] for error in errors)
    batch_engine.get_curation_batch.assert_not_awaited()
    batch_engine.revert_curation_batch.assert_not_awaited()
    batch_engine.apply_curation_batch.assert_not_awaited()


@pytest.mark.asyncio
async def test_static_preview_route_still_previews(batch_client: httpx.AsyncClient, batch_engine: MagicMock) -> None:
    response = await batch_client.post(
        ROOT + "/preview", json={"protocol": "raw-curation-v2", "memory_ids": [MEMORY_ID]}
    )
    assert response.status_code == 200, response.text
    assert response.json() == batch_engine.preview_curation_batch.return_value.model_dump(mode="json")
    batch_engine.preview_curation_batch.assert_awaited_once()
    batch_engine.apply_curation_batch.assert_not_awaited()


@pytest.mark.asyncio
async def test_apply_manifest_cannot_use_static_preview_route(
    batch_client: httpx.AsyncClient, batch_engine: MagicMock
) -> None:
    response = await batch_client.post(ROOT + "/preview", json=apply_request().model_dump(mode="json"))
    assert response.status_code == 422, response.text
    batch_engine.preview_curation_batch.assert_not_awaited()
    batch_engine.apply_curation_batch.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("batch_id", ["Preview", "previews", "preview-1", "preview_1"])
async def test_only_exact_preview_is_reserved_over_http(
    batch_client: httpx.AsyncClient, batch_engine: MagicMock, batch_id: str
) -> None:
    response = await batch_client.post(ROOT + "/" + batch_id, json=apply_request().model_dump(mode="json"))
    assert response.status_code == 409, response.text
    assert response.json() == {"detail": "test manifest conflict"}
    batch_engine.apply_curation_batch.assert_awaited_once()
    assert batch_engine.apply_curation_batch.await_args.args[1] == batch_id
    batch_engine.preview_curation_batch.assert_not_awaited()


def test_openapi_declares_curation_conflict_response(batch_engine: MagicMock) -> None:
    schema = create_app(batch_engine, initialize_memory=False).openapi()
    routes = [
        ("/v1/default/banks/{bank_id}/curation-batches/preview", "post"),
        ("/v1/default/banks/{bank_id}/curation-batches/{batch_id}", "post"),
        ("/v1/default/banks/{bank_id}/curation-batches/{batch_id}", "get"),
        ("/v1/default/banks/{bank_id}/curation-batches/{batch_id}/revert", "post"),
    ]
    response_schema = schema["components"]["schemas"]["CurationConflictResponse"]
    assert response_schema["properties"]["detail"] == {"type": "string", "title": "Detail"}
    for path, method in routes:
        conflict = schema["paths"][path][method]["responses"]["409"]
        assert conflict["content"]["application/json"]["schema"] == {
            "$ref": "#/components/schemas/CurationConflictResponse"
        }


def test_openapi_reserves_preview_in_every_batch_id_schema(batch_engine: MagicMock) -> None:
    schema = create_app(batch_engine, initialize_memory=False).openapi()
    schemas = [schema["components"]["schemas"]["CurationReceipt"]["properties"]["batch_id"]]
    batch_path = "/v1/default/banks/{bank_id}/curation-batches/{batch_id}"
    for path, method in [(batch_path, "post"), (batch_path, "get"), (batch_path + "/revert", "post")]:
        schemas.extend(
            parameter["schema"]
            for parameter in schema["paths"][path][method]["parameters"]
            if parameter["name"] == "batch_id"
        )
    assert len(schemas) == 4
    for batch_id_schema in schemas:
        assert batch_id_schema["not"] == {"enum": ["preview"]}
        assert "reserved" in batch_id_schema["description"]
