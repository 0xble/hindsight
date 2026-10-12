"""The fork installer must download its fork-owned launcher and budget helpers."""

from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[3]


def test_installer_downloads_the_complete_fork_script_set():
    source = (ROOT / "hindsight-docs/static/get-codex").read_text()
    raw = re.search(r'^GITHUB_RAW="([^"]+)"', source, re.MULTILINE)
    assert raw is not None
    assert raw.group(1) == "https://raw.githubusercontent.com/0xble/hindsight/main/hindsight-integrations/codex"
    scripts = re.search(r"SCRIPT_FILES=\((.*?)\)", source, re.DOTALL)
    assert scripts is not None
    paths = re.findall(r'"(scripts/[^"]+)"', scripts.group(1))
    assert {"scripts/recall_launcher.py", "scripts/lib/recall_context.py", "scripts/lib/token_budget.py"} <= set(paths)
    for path in paths:
        assert (ROOT / "hindsight-integrations/codex" / path).is_file(), path
