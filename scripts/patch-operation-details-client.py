"""Restore wire-discriminator validation after OpenAPI Generator emits a oneOf wrapper."""

from pathlib import Path

model = Path(__file__).resolve().parent.parent / "hindsight-clients/python/hindsight_client_api/models/operation_response_details.py"
text = model.read_text()
old_import = "from pydantic import BaseModel, ConfigDict, Field, StrictStr, ValidationError, field_validator\n"
new_import = old_import.rstrip() + ", model_validator\n"
anchor = "    def __init__(self, *args, **kwargs) -> None:\n"
validator = '''    @model_validator(mode='before')
    @classmethod
    def validate_wire_details(cls, value):
        # Generated oneOf wrappers otherwise ignore the raw wire fields and
        # construct actual_instance=None when nested in operation responses.
        if isinstance(value, dict) and 'actual_instance' not in value:
            operation_type = value.get('operation_type')
            variants = {
                'file_convert_retain': FileConvertRetainOperationDetails,
                'refresh_mental_model': RefreshMentalModelOperationDetails,
            }
            if operation_type not in variants:
                raise ValueError(f'Unknown operation details type: {operation_type!r}')
            return {'actual_instance': variants[operation_type].model_validate(value)}
        return value

'''
if old_import not in text or text.count(anchor) != 1 or validator in text:
    raise SystemExit("OpenAPI Generator wrapper changed: update discriminator patch")
model.write_text(text.replace(old_import, new_import, 1).replace(anchor, validator + anchor, 1))
