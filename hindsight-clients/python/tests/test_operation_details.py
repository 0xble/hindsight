"""Generated operation clients accept both supported detail variants."""

import pytest

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
