"""Exercise installed launcher interpreter selection through real processes."""

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.fixture
def installed_scripts(tmp_path):
    # Spaces exercise argv handling rather than shell interpolation.
    scripts = tmp_path / "installed integration" / "scripts"
    scripts.mkdir(parents=True)
    source = Path(__file__).resolve().parents[1] / "scripts/recall_launcher.py"
    shutil.copyfile(source, scripts / "recall_launcher.py")
    (scripts / "recall.py").write_text(
        "import json, sys\n"
        "print(json.dumps({'hookSpecificOutput': {'hookEventName': 'UserPromptSubmit', "
        "'additionalContext': 'fallback context'}, 'input': json.load(sys.stdin), "
        "'interpreter': sys.executable, 'arguments': sys.argv[1:]}))\n"
    )
    return scripts


def run_launcher(scripts):
    return subprocess.run(
        [sys.executable, str(scripts / "recall_launcher.py"), "argument with spaces"],
        input=json.dumps({"prompt": "Test recall through stdin"}),
        text=True,
        capture_output=True,
        timeout=10,
    )


@pytest.mark.parametrize("managed_state", ["absent", "dangling", "usable", "broken-startup"])
def test_launcher_selects_usable_interpreter_before_recall(installed_scripts, managed_state):
    managed = installed_scripts.parent / "tokenizer-venv/bin/python"
    if managed_state != "absent":
        managed.parent.mkdir(parents=True)
    if managed_state == "dangling":
        managed.symlink_to(installed_scripts.parent / "retired-python")
    elif managed_state == "usable":
        managed.symlink_to(sys.executable)
    elif managed_state == "broken-startup":
        managed.write_text("#!/bin/sh\nprintf 'hidden startup output'\nexit 1\n")
        managed.chmod(0o755)

    result = run_launcher(installed_scripts)
    assert result.returncode == 0
    assert result.stderr == ""
    output = json.loads(result.stdout)
    assert output["hookSpecificOutput"]["additionalContext"] == "fallback context"
    assert output["input"] == {"prompt": "Test recall through stdin"}
    assert output["arguments"] == ["argument with spaces"]
    expected_interpreter = str(managed) if managed_state == "usable" else sys.executable
    assert output["interpreter"] == expected_interpreter


def test_launcher_never_retries_a_started_recall(installed_scripts):
    managed = installed_scripts.parent / "tokenizer-venv/bin/python"
    managed.parent.mkdir(parents=True)
    managed.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "-c" ]; then exit 0; fi\n'
        "printf 'managed recall started once'\n"
        "exit 7\n"
    )
    managed.chmod(0o755)
    result = run_launcher(installed_scripts)
    assert result.returncode == 7
    assert result.stdout == "managed recall started once"
    assert result.stderr == ""


def test_installer_rebuilds_dangling_task_owned_venv(tmp_path):
    integration = Path(__file__).resolve().parents[1]
    installer = (integration.parents[1] / "hindsight-docs/static/get-codex").read_text()
    creation_command = re.search(r'(python3 -m venv [^\n]+?) &&', installer).group(1)
    tokenizer = tmp_path / "installed integration" / "tokenizer-venv"
    executable = tokenizer / "bin/python"
    executable.parent.mkdir(parents=True)
    executable.symlink_to(tmp_path / "retired-base-python")
    stale_marker = tokenizer / "obsolete-base-marker"
    stale_marker.write_text("old venv contents")

    # Run the installer's real venv creation command. Suppress ensurepip here
    # because this regression is interpreter repair, independent of downloads.
    creation = subprocess.run(
        ["bash", "-c", 'TOKENIZER_DIR="$1"; ' + creation_command + " --without-pip", "installer-test", str(tokenizer)],
        text=True, capture_output=True, timeout=30,
    )
    assert creation.returncode == 0, creation.stderr
    repaired = subprocess.run(
        [str(executable), "-c", "import sys; print(sys.executable)"],
        text=True, capture_output=True, timeout=10,
    )
    assert repaired.returncode == 0, repaired.stderr
    assert not stale_marker.exists()


def test_installer_does_not_clear_redirected_tokenizer_directory(tmp_path):
    integration = Path(__file__).resolve().parents[1]
    installer = (integration.parents[1] / "hindsight-docs/static/get-codex").read_text()
    creation_guard = re.search(r'^if (.+python3 -m venv [^\n]+?) &&', installer, re.MULTILINE).group(1)
    unrelated = tmp_path / "unrelated runtime"
    unrelated.mkdir()
    marker = unrelated / "preserve-me"
    marker.write_text("unrelated data")
    tokenizer = tmp_path / "tokenizer-venv"
    tokenizer.symlink_to(unrelated, target_is_directory=True)
    creation = subprocess.run(
        ["bash", "-c", 'TOKENIZER_DIR="$1"; ' + creation_guard + " --without-pip", "installer-test", str(tokenizer)],
        text=True, capture_output=True, timeout=30,
    )
    assert creation.returncode != 0
    assert marker.read_text() == "unrelated data"
