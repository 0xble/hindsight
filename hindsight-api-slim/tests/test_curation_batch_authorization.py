"""Authorization boundary for bank-wide raw-curation-v2 operations."""

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from hindsight_api import RequestContext
from hindsight_api.engine.curation_batch import (
    CurationApplyRequest,
    CurationChange,
    CurationPreviewRequest,
    CurationRevertRequest,
)
from hindsight_api.engine.memory_engine import MemoryEngine
from hindsight_api.extensions import OperationValidationError, ValidationResult


class ScopedValidator:
    def __init__(self, read_scope, write_scope):
        self.read_scope = read_scope
        self.write_scope = write_scope
        self.validate_bank_read = AsyncMock(return_value=ValidationResult.accept())
        self.validate_bank_write = AsyncMock(return_value=ValidationResult.accept())
        self.validate_memory_update = AsyncMock(return_value=ValidationResult.accept())

    async def resolve_tag_scope(self, ctx):
        return self.read_scope

    async def resolve_write_tag_scope(self, ctx):
        return self.write_scope


@pytest.fixture
def curation_engine():
    engine = object.__new__(MemoryEngine)
    engine._operation_validator = None
    engine._database_backend_type = "postgresql"
    engine._authenticate_tenant = AsyncMock()
    engine._get_backend = AsyncMock(side_effect=AssertionError("backend was reached"))
    engine._reembed_memory_text = AsyncMock(side_effect=AssertionError("provider was reached"))
    return engine


async def _invoke(engine, operation, context):
    if operation == "preview":
        return await engine.preview_curation_batch(
            "bank",
            CurationPreviewRequest(protocol="raw-curation-v2", memory_ids=[uuid4()]),
            request_context=context,
        )
    if operation == "get":
        return await engine.get_curation_batch("bank", "batch", request_context=context)
    if operation == "apply":
        return await engine.apply_curation_batch(
            "bank",
            "batch",
            CurationApplyRequest(
                protocol="raw-curation-v2",
                expected_closure_revision="0" * 64,
                changes=[
                    CurationChange(
                        memory_id=uuid4(),
                        memory_revision="0" * 64,
                        source_revision="0" * 64,
                        action="invalidate",
                        reason="test",
                    )
                ],
            ),
            request_context=context,
        )
    return await engine.revert_curation_batch(
        "bank",
        "batch",
        CurationRevertRequest(protocol="raw-curation-v2", expected_receipt_revision="0" * 64),
        request_context=context,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("read_scope", "write_scope"),
    [
        ([{"tags": ["tenant:a"], "match": "all_strict"}], None),
        (None, ["tenant:a"]),
        (None, []),
    ],
    ids=["read-scoped", "write-scoped", "read-only-write"],
)
@pytest.mark.parametrize("operation", ["preview", "get", "apply", "revert"])
async def test_scoped_batch_curation_is_rejected_before_storage(curation_engine, read_scope, write_scope, operation):
    curation_engine._operation_validator = ScopedValidator(read_scope, write_scope)
    context = RequestContext()
    with patch(
        "hindsight_api.engine.memories.get_memories", side_effect=AssertionError("storage was reached")
    ) as storage:
        with pytest.raises(OperationValidationError) as denied:
            await _invoke(curation_engine, operation, context)
    assert denied.value.status_code == 403
    storage.assert_not_called()
    curation_engine._get_backend.assert_not_awaited()
    curation_engine._reembed_memory_text.assert_not_awaited()
    curation_engine._authenticate_tenant.assert_awaited_once_with(context)


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["preview", "get", "apply", "revert"])
@pytest.mark.parametrize("validator_enabled", [False, True])
async def test_unscoped_batch_curation_reaches_storage(curation_engine, operation, validator_enabled):
    if validator_enabled:
        curation_engine._operation_validator = ScopedValidator(None, None)
    store = MagicMock()
    store.store_owned_for.return_value = False
    context = RequestContext()
    with (
        patch("hindsight_api.engine.memories.get_memories", return_value=store),
        patch.object(curation_engine, "_require_writable", wraps=curation_engine._require_writable) as writable,
    ):
        with pytest.raises(AssertionError, match="backend was reached"):
            await _invoke(curation_engine, operation, context)
    curation_engine._get_backend.assert_awaited_once()
    if operation in ("apply", "revert"):
        writable.assert_awaited_once_with("bank", context)
    else:
        writable.assert_not_awaited()
