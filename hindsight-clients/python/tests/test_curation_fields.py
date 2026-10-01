"""Generated patch bodies must retain omission versus explicit null."""

from hindsight_client_api.models.curation_fields import CurationFields


def test_nullable_correction_fields_preserve_presence_through_json_loading():
    untouched = CurationFields.from_dict({"text": "canonical correction"})
    assert untouched.to_dict() == {"text": "canonical correction"}
    clear = CurationFields.from_dict({"text": "canonical correction", "context": None, "occurred_start": None})
    assert clear.to_dict() == {"text": "canonical correction", "context": None, "occurred_start": None}
    assert CurationFields(context=None).to_dict() == {"context": None}
