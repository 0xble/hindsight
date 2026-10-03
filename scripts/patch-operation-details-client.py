"""Restore nullable wire-discriminator decoding in generated Python and Go clients.

Run after each language's generation. Reapplying is byte-idempotent; unexpected
wrapper layouts fail closed instead of silently shipping unpatched decoders.
"""

import argparse
import re
from pathlib import Path

PYTHON_VALIDATOR = """    @model_validator(mode='before')
    @classmethod
    def validate_wire_details(cls, value: Any) -> Any:
        # Generated oneOf wrappers otherwise ignore raw wire fields when nested,
        # and trial-decode JSON null as a successful match for EVERY variant.
        if value is None:
            return {'actual_instance': None}
        if isinstance(value, dict) and 'actual_instance' not in value:
            operation_type = value.get('operation_type')
            variants = {
                'file_convert_retain': FileConvertRetainOperationDetails,
                'refresh_mental_model': RefreshMentalModelOperationDetails,
            }
            if not isinstance(operation_type, str) or operation_type not in variants:
                raise ValueError(f'Unknown operation details type: {operation_type!r}')
            return {'actual_instance': variants[operation_type].model_validate(value)}
        return value

"""

PYTHON_DESERIALIZERS = '''    @classmethod
    def from_dict(cls, obj: Optional[Union[str, Dict[str, Any]]]) -> Self:
        return cls.model_validate(obj)

    @classmethod
    def from_json(cls, json_str: Optional[str]) -> Self:
        """Decode null once, otherwise select by the wire discriminator."""
        return cls.from_dict(None if json_str is None else json.loads(json_str))

'''

GO_UNMARSHAL = """func (dst *OperationResponseDetails) UnmarshalJSON(data []byte) error {
	// Trial-decoding by shape accepts mismatched operation_type values and
	// treats null as matching both schemas. Null is an empty union; all other
	// payloads must select exactly the variant named on the wire.
	*dst = OperationResponseDetails{}
	if bytes.Equal(bytes.TrimSpace(data), []byte("null")) {
		return nil
	}
	var tag struct {
		OperationType string `json:"operation_type"`
	}
	if err := json.Unmarshal(data, &tag); err != nil {
		return err
	}
	switch tag.OperationType {
	case "file_convert_retain":
		var detail FileConvertRetainOperationDetails
		if err := json.Unmarshal(data, &detail); err != nil {
			return err
		}
		dst.FileConvertRetainOperationDetails = &detail
	case "refresh_mental_model":
		var detail RefreshMentalModelOperationDetails
		if err := json.Unmarshal(data, &detail); err != nil {
			return err
		}
		dst.RefreshMentalModelOperationDetails = &detail
	default:
		return fmt.Errorf("unknown operation details type: %q", tag.OperationType)
	}
	return nil
}

"""


def replace_region(text: str, pattern: str, replacement: str) -> str:
    """Require one known generator boundary, including on subsequent runs."""
    text, count = re.subn(pattern, lambda _: replacement, text, flags=re.DOTALL)
    if count != 1:
        raise SystemExit("OpenAPI Generator wrapper changed: update discriminator patch")
    return text


def patch_python(model: Path) -> None:
    text = model.read_text()
    old_import = "from pydantic import BaseModel, ConfigDict, Field, StrictStr, ValidationError, field_validator\n"
    new_import = old_import.rstrip() + ", model_validator\n"
    if old_import in text:
        text = text.replace(old_import, new_import, 1)
    elif new_import not in text:
        raise SystemExit("OpenAPI Generator Python imports changed: update discriminator patch")
    anchor = "    def __init__(self, *args, **kwargs) -> None:\n"
    if text.count(anchor) != 1:
        raise SystemExit("OpenAPI Generator Python constructor changed: update discriminator patch")
    if "    def validate_wire_details(" in text:
        text = replace_region(text, r"    @model_validator\(mode='before'\).*?(?=    def __init__\()", PYTHON_VALIDATOR)
    else:
        text = text.replace(anchor, PYTHON_VALIDATOR + anchor, 1)
    text = replace_region(text, r"    @classmethod\n    def from_dict\(.*?(?=    def to_json\()", PYTHON_DESERIALIZERS)
    model.write_text(text)


def patch_go(model: Path) -> None:
    text = model.read_text()
    if '\t"gopkg.in/validator.v2"\n' in text:
        text = text.replace('\t"gopkg.in/validator.v2"\n', '\t"bytes"\n', 1)
    elif '\t"bytes"\n' not in text:
        raise SystemExit("OpenAPI Generator Go imports changed: update discriminator patch")
    text = replace_region(
        text,
        r"func \(dst \*OperationResponseDetails\) UnmarshalJSON\(.*?(?=// Marshal data)",
        GO_UNMARSHAL,
    )
    old_null = "return nil, nil // no data in oneOf schemas"
    new_null = 'return []byte("null"), nil // no data in oneOf schemas'
    if old_null in text:
        text = text.replace(old_null, new_null, 1)
    elif new_null not in text:
        raise SystemExit("OpenAPI Generator Go null serializer changed: update discriminator patch")
    model.write_text(text)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--language", choices=("python", "go", "all"), default="all")
    parser.add_argument("--project-root", type=Path, default=Path(__file__).resolve().parent.parent)
    args = parser.parse_args()
    clients = args.project_root / "hindsight-clients"
    if args.language in ("python", "all"):
        patch_python(clients / "python/hindsight_client_api/models/operation_response_details.py")
    if args.language in ("go", "all"):
        patch_go(clients / "go/model_operation_response_details.go")


if __name__ == "__main__":
    main()
