"""Generated operation clients accept both supported detail variants."""

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from hindsight_client_api.models.file_convert_retain_operation_details import (
    FileConvertRetainOperationDetails,
)
from hindsight_client_api.models.operation_response import OperationResponse
from hindsight_client_api.models.operation_response_details import OperationResponseDetails
from hindsight_client_api.models.operation_status_response import OperationStatusResponse
from hindsight_client_api.models.refresh_mental_model_operation_details import (
    RefreshMentalModelOperationDetails,
)


@pytest.mark.parametrize(
    ("payload", "expected_type"),
    [
        (
            {
                "operation_type": "file_convert_retain",
                "failure_class": "low_quality_ocr",
                "failure_reason": "no_meaningful_text",
            },
            FileConvertRetainOperationDetails,
        ),
        (
            {
                "operation_type": "file_convert_retain",
                "failure_class": "no_extractable_text",
                "failure_reason": "empty_content",
                "parsers": ["markitdown"],
            },
            FileConvertRetainOperationDetails,
        ),
        (
            {
                "operation_type": "refresh_mental_model",
                "outcome": "content_written",
                "failure_reason": None,
            },
            RefreshMentalModelOperationDetails,
        ),
    ],
)
def test_operation_details_deserialize_by_discriminator(payload, expected_type):
    details = OperationResponseDetails.from_dict(payload)

    assert isinstance(details.actual_instance, expected_type)
    assert isinstance(OperationResponseDetails.model_validate(payload).actual_instance, expected_type)
    assert isinstance(OperationResponseDetails.model_validate_json(json.dumps(payload)).actual_instance, expected_type)
    status = OperationStatusResponse.model_validate({"operation_id": "op", "status": "completed", "details": payload})
    assert status.details is not None
    assert isinstance(status.details.actual_instance, expected_type)
    response = OperationResponse.model_validate(
        {
            "id": "op",
            "task_type": "test",
            "items_count": 1,
            "created_at": "2026-09-27T00:00:00Z",
            "status": "completed",
            "error_message": None,
            "details": payload,
        }
    )
    assert response.details is not None
    assert isinstance(response.details.actual_instance, expected_type)
    if expected_type is FileConvertRetainOperationDetails:
        assert response.details.actual_instance.failure_reason == payload["failure_reason"]
        assert response.details.actual_instance.model_dump(mode="json")["failure_reason"] == payload["failure_reason"]


@pytest.mark.parametrize("json_value", [None, "null", " \n null "])
def test_operation_detail_null_json(json_value: str | None):
    details = OperationResponseDetails.from_json(json_value)
    assert details.actual_instance is None
    assert details.to_dict() is None
    assert details.to_json() == "null"


def test_operation_detail_null_dict():
    details = OperationResponseDetails.from_dict(None)
    assert details.actual_instance is None
    assert details.to_json() == "null"


@pytest.mark.parametrize("model", [OperationResponse, OperationStatusResponse])
@pytest.mark.parametrize("include_details", [True, False])
def test_parent_operation_preserves_nullable_details(model, include_details: bool):
    payload = {
        "id": "op",
        "operation_id": "op",
        "task_type": "test",
        "items_count": 1,
        "created_at": "2026-09-27T00:00:00Z",
        "status": "completed",
        "error_message": None,
    }
    if include_details:
        payload["details"] = None
    assert model.from_dict(payload).details is None
    assert model.model_validate(payload).details is None
    assert model.model_validate_json(json.dumps(payload)).details is None


@pytest.mark.parametrize(
    "payload",
    [
        {},
        "",
        {"operation_type": "unknown", "outcome": "content_written"},
        {"outcome": "content_written"},
        {"operation_type": None, "outcome": "content_written"},
        {"operation_type": "file_convert_retain", "outcome": "content_written"},
        {
            "operation_type": "refresh_mental_model",
            "failure_class": "low_quality_ocr",
            "failure_reason": "no_meaningful_text",
        },
    ],
)
@pytest.mark.parametrize("method", ["from_dict", "from_json", "model_validate", "model_validate_json"])
def test_operation_details_reject_unknown_or_mismatched_discriminator(payload, method: str):
    value = json.dumps(payload) if method.endswith("json") else payload
    with pytest.raises(ValueError):
        getattr(OperationResponseDetails, method)(value)
    with pytest.raises(ValueError):
        OperationStatusResponse.model_validate({"operation_id": "op", "status": "failed", "details": payload})


@pytest.mark.parametrize("language", ["python", "go"])
def test_operation_detail_generation_patch_is_idempotent(tmp_path: Path, language: str):
    project_root = Path(__file__).resolve().parents[3]
    model_path = {
        "python": "hindsight-clients/python/hindsight_client_api/models/operation_response_details.py",
        "go": "hindsight-clients/go/model_operation_response_details.go",
    }[language]
    target = tmp_path / model_path
    target.parent.mkdir(parents=True)
    shutil.copyfile(project_root / model_path, target)
    original = target.read_bytes()
    command = [
        sys.executable,
        str(project_root / "scripts/patch-operation-details-client.py"),
        "--language",
        language,
        "--project-root",
        str(tmp_path),
    ]
    for _ in range(2):
        subprocess.run(command, check=True, capture_output=True)
        assert target.read_bytes() == original

    # A changed generator boundary must fail without overwriting the model.
    target.write_text(
        target.read_text().replace("def from_dict(", "def renamed_from_dict(")
        if language == "python"
        else target.read_text().replace("// Marshal data", "// renamed marshal boundary")
    )
    changed = target.read_bytes()
    result = subprocess.run(command, check=False, capture_output=True, text=True)
    assert result.returncode != 0
    assert "wrapper changed" in result.stderr
    assert target.read_bytes() == changed
