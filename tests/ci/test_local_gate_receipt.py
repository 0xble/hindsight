from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from datetime import datetime
from pathlib import Path


BIN_CI = Path(__file__).resolve().parents[2] / "bin" / "ci"


def receipt_function() -> str:
    source = BIN_CI.read_text(encoding="utf-8")
    start = source.index("write_local_receipt() {")
    end = source.index("\nworkflow_policy() {", start)
    return source[start:end]


class LocalGateReceiptTests(unittest.TestCase):
    def make_repo(self, origin: str) -> tuple[tempfile.TemporaryDirectory[str], Path, str]:
        temp = tempfile.TemporaryDirectory()
        root = Path(temp.name)
        repo = root / "repo"
        repo.mkdir()
        subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.email", "ci@example.test"], check=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.name", "CI Test"], check=True)
        (repo / "tracked.txt").write_text("receipt test\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(repo), "add", "tracked.txt"], check=True)
        subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "test receipt"], check=True)
        subprocess.run(["git", "-C", str(repo), "switch", "-q", "-c", "receipt-test"], check=True)
        subprocess.run(["git", "-C", str(repo), "remote", "add", "origin", origin], check=True)
        sha = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
        return temp, repo, sha

    def run_receipt_helper(self, origin: str, github_actions: str | None = None) -> tuple[Path, str]:
        temp, repo, sha = self.make_repo(origin)
        self.addCleanup(temp.cleanup)
        receipt_root = Path(temp.name) / "receipts"
        helper = Path(temp.name) / "receipt-helper.sh"
        helper.write_text(
            "#!/usr/bin/env bash\n"
            "set -euo pipefail\n"
            "cd \"$1\"\n"
            f"{receipt_function()}\n"
            'if [[ "${GITHUB_ACTIONS:-}" != true ]]; then\n'
            '  write_local_receipt "$2"\n'
            "fi\n",
            encoding="utf-8",
        )
        env = os.environ.copy()
        env["CI_RECEIPT_ROOT"] = str(receipt_root)
        if github_actions is not None:
            env["GITHUB_ACTIONS"] = github_actions
        else:
            env.pop("GITHUB_ACTIONS", None)
        subprocess.run(
            ["/bin/bash", str(helper), str(repo), sha],
            check=True,
            capture_output=True,
            text=True,
            env=env,
        )
        return receipt_root, sha

    def test_https_origin_writes_lowercase_exact_sha_receipt(self) -> None:
        receipt_root, sha = self.run_receipt_helper("https://GitHub.com/Owner/Repo.git")
        receipt = receipt_root / "owner" / "repo" / f"{sha}.json"
        self.assertTrue(receipt.is_file())
        record = json.loads(receipt.read_text(encoding="utf-8"))
        self.assertEqual(
            record,
            {
                "schema_version": 1,
                "repository": "owner/repo",
                "sha": sha,
                "branch": "receipt-test",
                "command": "./bin/ci gate",
                "gate_exit": 0,
                "clean_tracked_tree": True,
                "timestamp": record["timestamp"],
            },
        )
        self.assertTrue(record["timestamp"].endswith("Z"))
        datetime.fromisoformat(record["timestamp"].replace("Z", "+00:00"))

    def test_ssh_origin_writes_lowercase_exact_sha_receipt(self) -> None:
        receipt_root, sha = self.run_receipt_helper("git@github.com:Owner/Repo.git")
        receipt = receipt_root / "owner" / "repo" / f"{sha}.json"
        self.assertTrue(receipt.is_file())
        record = json.loads(receipt.read_text(encoding="utf-8"))
        self.assertEqual(record["repository"], "owner/repo")
        self.assertEqual(record["sha"], sha)
        self.assertEqual(record["branch"], "receipt-test")

    def test_github_actions_skips_receipt(self) -> None:
        receipt_root, _ = self.run_receipt_helper("git@github.com:Owner/Repo.git", github_actions="true")
        self.assertFalse(receipt_root.exists())

    def test_helper_is_portable_bash_not_parameter_lowercase(self) -> None:
        self.assertNotIn(",,}", receipt_function())


if __name__ == "__main__":
    unittest.main()
