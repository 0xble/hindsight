"""Keep generated Python curation patches JSON-serializable.

OpenAPI Generator's Python model keeps datetime objects in ``to_dict`` so its
legacy ``to_json`` wrapper raises TypeError. Run this post-generation patch after
each Python client regeneration; it is byte-idempotent and fails closed when the
generator changes the model layout.
"""

import argparse
from pathlib import Path


OLD = """        _dict = self.model_dump(
            by_alias=True,
            exclude=excluded_fields,
            exclude_none=True,
        )
"""
NEW = """        _dict = self.model_dump(
            mode=\"json\",
            by_alias=True,
            exclude=excluded_fields,
            exclude_none=True,
        )
"""


def patch(model: Path) -> None:
    text = model.read_text()
    if NEW in text:
        return
    if OLD not in text:
        raise SystemExit("OpenAPI Generator CurationFields.to_dict layout changed: update datetime patch")
    model.write_text(text.replace(OLD, NEW, 1))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", type=Path, default=Path(__file__).resolve().parent.parent)
    args = parser.parse_args()
    patch(args.project_root / "hindsight-clients/python/hindsight_client_api/models/curation_fields.py")


if __name__ == "__main__":
    main()
