"""Offline tokenizer fallback and managed-runtime recall boundary checks.

Run managed checks with tiktoken==0.12.0 and CODEX_TEST_O200K_ASSET pointing
at the installer-downloaded encoding asset. No test downloads assets.
"""

import json
import os
from importlib.metadata import PackageNotFoundError
from pathlib import Path
from unittest.mock import patch

import pytest

from conftest import FakeHTTPResponse, make_hook_input, make_memory
from lib.token_budget import load_token_budget
from test_hooks import _run_hook


def test_missing_dependency_uses_byte_upper_bound(tmp_path):
    with patch("lib.token_budget.version", side_effect=PackageNotFoundError):
        counter = load_token_budget(tmp_path / "missing")
    assert counter.method == "utf8-byte-upper-bound"
    assert counter.count("日本語🧠") == len("日本語🧠".encode("utf-8"))


@pytest.mark.parametrize("contents", [None, b"corrupt encoding"])
def test_missing_or_corrupt_asset_never_fetches_network(tmp_path, contents):
    asset = tmp_path / "encoding"
    if contents is not None:
        asset.write_bytes(contents)
    with (
        patch("lib.token_budget.version", return_value="0.12.0"),
        patch("socket.socket", side_effect=AssertionError("Hook must stay offline")),
    ):
        counter = load_token_budget(asset)
    assert counter.method == "utf8-byte-upper-bound"


@pytest.fixture
def managed_tokenizer():
    tiktoken = pytest.importorskip("tiktoken")
    asset_path = os.environ.get("CODEX_TEST_O200K_ASSET")
    if not asset_path:
        pytest.skip("Set CODEX_TEST_O200K_ASSET for managed tokenizer checks")
    counter = load_token_budget(Path(asset_path))
    assert counter.method == "o200k_base"
    return counter


def test_managed_encoding_matches_independent_o200k(managed_tokenizer):
    import tiktoken

    # The reference tokenizer can load its test cache. Production loader only
    # reads the verified local asset and never reaches get_encoding.
    reference = tiktoken.get_encoding("o200k_base")
    for text in ["Brian plans a migration, but has not deployed.", "日本語 🧠", "<|endoftext|>"]:
        assert managed_tokenizer.count(text) == len(reference.encode(text, disallowed_special=()))


def test_managed_hook_fills_real_token_budget_offline(managed_tokenizer, monkeypatch, tmp_path):
    memories = [
        make_memory(f"Decision {i}: Brian proposed a migration. Mahin has not agreed and it remains a plan.")
        for i in range(80)
    ]
    response = FakeHTTPResponse({"results": memories})
    with (
        patch("lib.recall_context.load_token_budget", return_value=managed_tokenizer),
        patch("socket.socket", side_effect=AssertionError("Hook must stay offline")),
    ):
        output = _run_hook(
            "recall", make_hook_input(), monkeypatch, tmp_path,
            urlopen_side_effect=lambda *a, **kw: response,
            user_config={"recallMaxTokens": 1024},
        )
    context = json.loads(output)["hookSpecificOutput"]["additionalContext"]
    assert 900 <= managed_tokenizer.count(context) <= 1024
    assert len(context.encode("utf-8")) > 1024
    included = [memory for memory in memories if memory["text"] in context]
    assert len(included) > 5
    for memory in included:
        assert memory["text"] + " [experience] (2024-01-15)" in context
    state = json.loads((tmp_path / ".hindsight/codex/state/last_recall.json").read_text())
    assert state["result_count"] == len(included)


def test_installer_downloads_every_hook_library_and_uses_resilient_launcher():
    integration = Path(__file__).resolve().parents[1]
    installer = (integration.parents[1] / "hindsight-docs/static/get-codex").read_text()
    for library in (integration / "scripts/lib").glob("*.py"):
        assert f'"scripts/lib/{library.name}"' in installer
    assert '"command": "python3 \\"${SCRIPTS_DIR}/recall_launcher.py\\""' in installer
    assert '"scripts/recall_launcher.py"' in installer
    assert "'tiktoken==0.12.0'" in installer


def test_managed_hook_wrapper_overflow_emits_no_context(managed_tokenizer, monkeypatch, tmp_path):
    response = FakeHTTPResponse({"results": [make_memory("Small fact")]})
    with patch("lib.recall_context.load_token_budget", return_value=managed_tokenizer):
        output = _run_hook(
            "recall", make_hook_input(), monkeypatch, tmp_path,
            urlopen_side_effect=lambda *a, **kw: response,
            user_config={"recallMaxTokens": 1024, "recallPromptPreamble": "🧠 " * 1500},
        )
    assert output == ""


def test_managed_hook_skips_large_fact_whole(managed_tokenizer, monkeypatch, tmp_path):
    memories = [
        make_memory("Oversized plan " + "日本語🧠 " * 2000 + ", but it never happened."),
        make_memory("Brian said the migration is still pending."),
    ]
    response = FakeHTTPResponse({"results": memories})
    with patch("lib.recall_context.load_token_budget", return_value=managed_tokenizer):
        output = _run_hook(
            "recall", make_hook_input(), monkeypatch, tmp_path,
            urlopen_side_effect=lambda *a, **kw: response,
        )
    context = json.loads(output)["hookSpecificOutput"]["additionalContext"]
    assert "Oversized plan" not in context
    assert "never happened" not in context
    assert memories[1]["text"] + " [experience] (2024-01-15)" in context
