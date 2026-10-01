#!/usr/bin/env python3
"""Select a usable recall interpreter before executing the hook once."""

import os
import subprocess
import sys
from pathlib import Path


def main() -> None:
    scripts_dir = Path(__file__).resolve().parent
    managed_python = scripts_dir.parent / "tokenizer-venv" / "bin" / "python"
    recall_script = scripts_dir / "recall.py"
    interpreter = sys.executable

    # A venv can outlive its base Python. Probe startup before selecting it,
    # so a dangling symlink or broken interpreter retains the byte fallback.
    # Never retry recall itself: it may have already made API/state writes.
    if os.access(managed_python, os.X_OK):
        try:
            probe = subprocess.run(
                [str(managed_python), "-c", "pass"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=2,
                check=False,
            )
            if probe.returncode == 0:
                interpreter = str(managed_python)
        except (OSError, subprocess.TimeoutExpired):
            pass

    os.execv(interpreter, [interpreter, str(recall_script), *sys.argv[1:]])


if __name__ == "__main__":
    main()
