"""Generated operation clients accept both supported detail variants."""

import json

import pytest

from hindsight_client_api.models.operation_response import OperationResponse
from hindsight_client_api.models.operation_status_response import OperationStatusResponse

from hindsight_client_api.models.file_convert_retain_operation_details import (
    FileConvertRetainOperationDetails,
)
from hindsight_client_api.models.operation_response_details import OperationResponseDetails
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
    response = OperationResponse.model_validate({
        "id": "op", "task_type": "test", "items_count": 1,
        "created_at": "2026-09-27T00:00:00Z", "status": "completed", "error_message": None,
        "details": payload,
    })
    assert response.details is not None
    assert isinstance(response.details.actual_instance, expected_type)
    if expected_type is FileConvertRetainOperationDetails:
        assert response.details.actual_instance.failure_reason == payload["failure_reason"]
        assert response.details.actual_instance.model_dump(mode="json")["failure_reason"] == payload["failure_reason"]
