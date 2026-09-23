"""Upstream synchronization must not restore hosted workflows."""

from pathlib import Path
import tempfile
import unittest

from scripts.ci.validate_fork_workflows import validate


class PolicyTests(unittest.TestCase):
    def test_no_workflows_passes(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(validate(Path(directory)), [])

    def test_restored_workflow_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workflows = root / ".github/workflows"
            workflows.mkdir(parents=True)
            (workflows / "upstream.yml").write_text("on: push\n")
            self.assertTrue(validate(root))

    def test_symlink_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".github").symlink_to(root)
            self.assertTrue(validate(root))

    def test_legacy_controller_contract_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".local-ci.json").write_text("{}")
            self.assertTrue(validate(root))


if __name__ == "__main__":
    unittest.main()
