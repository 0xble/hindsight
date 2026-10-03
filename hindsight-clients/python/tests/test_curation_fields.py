"""Generated patch bodies must retain omission versus explicit null."""

import json
from datetime import UTC, datetime

from hindsight_client_api.models.curation_fields import CurationFields


def test_nullable_correction_fields_preserve_presence_through_json_loading():
    untouched = CurationFields.from_dict({"text": "canonical correction"})
    assert untouched.to_dict() == {"text": "canonical correction"}
    clear = CurationFields.from_dict({"text": "canonical correction", "context": None, "occurred_start": None})
    assert clear.to_dict() == {"text": "canonical correction", "context": None, "occurred_start": None}
    assert CurationFields(context=None).to_dict() == {"context": None}


def test_datetime_fields_are_json_serializable():
    fields = CurationFields(
        occurred_start=datetime(2024, 1, 1, tzinfo=UTC),
        occurred_end=datetime(2024, 1, 2, tzinfo=UTC),
    )
    payload = json.loads(fields.to_json())
    assert payload == {
        "occurred_start": "2024-01-01T00:00:00Z",
        "occurred_end": "2024-01-02T00:00:00Z",
    }
