#!/usr/bin/env python3
"""Keep this fork's checks local when synchronizing upstream."""

import sys
from pathlib import Path


def validate(root: Path) -> list[str]:
    errors = []
    for path in (root / ".github", root / ".github/workflows"):
        if path.is_symlink():
            errors.append(f"CI policy rejects symlink: {path.relative_to(root)}")
            return errors
    workflows = root / ".github/workflows"
    if workflows.exists() and (not workflows.is_dir() or any(workflows.iterdir())):
        errors.append("GitHub Actions are disabled for this fork; maintain bin/ci instead")
    if (root / ".local-ci.json").exists() or (root / ".local-ci.json").is_symlink():
        errors.append("Legacy .local-ci.json must not replace the contributor entrypoint")
    return errors


if __name__ == "__main__":
    root = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parents[2]
    errors = validate(root)
    for error in errors:
        print(error, file=sys.stderr)
    raise SystemExit(bool(errors))
