from __future__ import annotations

import importlib.util
import os
import shlex
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "ci" / "validate_fork_workflows.py"
SPEC = importlib.util.spec_from_file_location("validate_fork_workflows", SCRIPT)
assert SPEC and SPEC.loader
POLICY = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(POLICY)


class ForkWorkflowPolicyTests(unittest.TestCase):
    def make_root(self) -> Path:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name) / "candidate"
        workflow_dir = root / ".github" / "workflows"
        workflow_dir.mkdir(parents=True)
        for name, events in POLICY.EXPECTED_EVENTS.items():
            if name == "fork-policy.yml":
                (workflow_dir / name).write_text(
                    yaml.safe_dump(POLICY.EXPECTED_FORK_POLICY_WORKFLOW, sort_keys=False),
                    encoding="utf-8",
                )
                continue
            if name == "fork-ci.yml":
                trigger = (
                    "on:\n  push:\n    branches: [main]\n  pull_request:\n    branches: [main]\n  workflow_dispatch:\n"
                )
            else:
                inline_events = ", ".join(sorted(events))
                trigger = f"on: [{inline_events}]\n"
            (workflow_dir / name).write_text(
                f"name: Test\n{trigger}permissions: {{contents: read}}\njobs:\n  test:\n    runs-on: ubuntu-latest\n    steps:\n      - run: true\n",
                encoding="utf-8",
            )
        return root

    def test_inline_on_syntax_is_parsed_and_allowed(self) -> None:
        self.assertEqual(POLICY.validate(self.make_root()), [])

    def test_restored_upstream_workflow_fails(self) -> None:
        root = self.make_root()
        (root / ".github" / "workflows" / "release.yml").write_text("name: Release\n", encoding="utf-8")
        errors = POLICY.validate(root)
        self.assertTrue(any("forbidden upstream workflows restored" in error for error in errors))

    def test_extra_workflow_file_fails(self) -> None:
        root = self.make_root()
        (root / ".github" / "workflows" / "surprise.yaml").write_text("name: Surprise\n", encoding="utf-8")
        errors = POLICY.validate(root)
        self.assertTrue(any("unapproved workflow entrypoints" in error for error in errors))

    def test_candidate_controlled_policy_paths_cannot_be_symlinks(self) -> None:
        policy_paths = (Path(".github"), Path(".github/workflows")) + tuple(
            Path(".github/workflows") / name for name in POLICY.EXPECTED_EVENTS
        )
        for policy_path in policy_paths:
            with self.subTest(policy_path=policy_path):
                root = self.make_root()
                link = root / policy_path
                trusted = root.parent / "trusted" / policy_path
                trusted.parent.mkdir(parents=True, exist_ok=True)
                link.rename(trusted)
                link.symlink_to(os.path.relpath(trusted, link.parent))

                errors = POLICY.validate(root)
                self.assertTrue(any("symbolic link" in error for error in errors), errors)

    def test_unapproved_workflow_entry_cannot_be_a_symlink(self) -> None:
        root = self.make_root()
        trusted = root.parent / "trusted" / "surprise.txt"
        trusted.parent.mkdir(parents=True)
        trusted.write_text("not a workflow\n", encoding="utf-8")
        (root / ".github" / "workflows" / "surprise.txt").symlink_to(
            os.path.relpath(trusted, root / ".github" / "workflows")
        )

        errors = POLICY.validate(root)
        self.assertTrue(any("symbolic link workflow entry" in error for error in errors), errors)

    def test_candidate_root_cannot_be_a_symlink(self) -> None:
        root = self.make_root()
        trusted = root.with_name("trusted")
        root.rename(trusted)
        root.symlink_to(trusted.name)

        errors = POLICY.validate(root)
        self.assertTrue(any("candidate root" in error and "symbolic link" in error for error in errors), errors)

    def test_inline_schedule_fails(self) -> None:
        root = self.make_root()
        workflow = root / ".github" / "workflows" / "perf-test.yml"
        workflow.write_text(
            workflow.read_text(encoding="utf-8").replace(
                "on: [workflow_dispatch]", "on: [workflow_dispatch, schedule]"
            ),
            encoding="utf-8",
        )
        errors = POLICY.validate(root)
        self.assertTrue(any("events" in error and "schedule" in error for error in errors))

    def test_manual_workflow_inputs_are_preserved(self) -> None:
        root = self.make_root()
        workflow = root / ".github" / "workflows" / "perf-test.yml"
        workflow.write_text(
            workflow.read_text(encoding="utf-8").replace(
                "on: [workflow_dispatch]",
                "on:\n  workflow_dispatch:\n    inputs:\n      suite:\n        type: string\n        required: false",
            ),
            encoding="utf-8",
        )
        self.assertEqual(POLICY.validate(root), [])

    def test_fork_ci_branch_broadening_fails(self) -> None:
        root = self.make_root()
        workflow = root / ".github" / "workflows" / "fork-ci.yml"
        workflow.write_text(
            workflow.read_text(encoding="utf-8").replace("branches: [main]", "branches: [main, develop]", 1),
            encoding="utf-8",
        )
        errors = POLICY.validate(root)
        self.assertTrue(any("trigger configuration" in error for error in errors))

    def test_candidate_cannot_select_executed_policy_code(self) -> None:
        root = self.make_root()
        candidate_validator = root / "scripts" / "ci" / "validate_fork_workflows.py"
        candidate_validator.parent.mkdir(parents=True)
        candidate_validator.write_text("raise SystemExit(0)\n", encoding="utf-8")

        workflow = root / ".github" / "workflows" / "fork-policy.yml"
        candidate_policy = POLICY.load_workflow(workflow)
        candidate_policy["jobs"]["policy"]["steps"][-1]["run"] = (
            "python candidate/scripts/ci/validate_fork_workflows.py candidate"
        )
        workflow.write_text(yaml.safe_dump(candidate_policy, sort_keys=False), encoding="utf-8")

        errors = POLICY.validate(root)
        self.assertTrue(any("trusted policy workflow" in error for error in errors), errors)

    def test_policy_workflow_pins_trusted_code_and_immutable_candidate(self) -> None:
        workflow = POLICY.EXPECTED_FORK_POLICY_WORKFLOW
        self.assertEqual(workflow["on"], {"pull_request_target": {"branches": ["main"]}})
        self.assertEqual(workflow["permissions"], {"contents": "read"})
        steps = workflow["jobs"]["policy"]["steps"]
        self.assertEqual(steps[0]["with"]["ref"], "${{ github.event.repository.default_branch }}")
        self.assertEqual(steps[0]["with"]["persist-credentials"], False)
        self.assertEqual(steps[1]["with"]["ref"], "${{ github.event.pull_request.head.sha }}")
        self.assertEqual(steps[1]["with"]["persist-credentials"], False)
        run_steps = [step["run"] for step in steps if "run" in step]
        self.assertTrue(all("trusted/" in command for command in run_steps), run_steps)
        self.assertTrue(all("candidate/scripts" not in command for command in run_steps), run_steps)

    def test_policy_workflow_trusted_commands_resolve_from_uv_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            project = root / "trusted" / "hindsight-api-slim"
            tests = root / "trusted" / "tests" / "ci"
            scripts = root / "trusted" / "scripts" / "ci"
            candidate = root / "candidate"
            for directory in (project, tests, scripts, candidate / ".github" / "workflows"):
                directory.mkdir(parents=True)

            (project / "pyproject.toml").write_text(
                '[project]\nname = "trusted-command-simulation"\nversion = "0.0.0"\nrequires-python = ">=3.11"\n',
                encoding="utf-8",
            )
            subprocess.run(["uv", "lock", "--directory", str(project)], check=True, capture_output=True, text=True)
            (tests / "test_validate_fork_workflows.py").write_text(
                'from pathlib import Path\nassert Path.cwd().name == "hindsight-api-slim"\n',
                encoding="utf-8",
            )
            (scripts / "validate_fork_workflows.py").write_text(
                "from pathlib import Path\n"
                "import sys\n"
                "assert Path(sys.argv[1]).resolve() == (Path.cwd().parents[1] / 'candidate').resolve()\n",
                encoding="utf-8",
            )

            commands = (
                "uv run --directory trusted/hindsight-api-slim --frozen python ../tests/ci/test_validate_fork_workflows.py",
                "uv run --directory trusted/hindsight-api-slim --frozen python ../scripts/ci/validate_fork_workflows.py ../../candidate",
            )
            steps = POLICY.EXPECTED_FORK_POLICY_WORKFLOW["jobs"]["policy"]["steps"]
            self.assertEqual(tuple(step["run"] for step in steps if "run" in step), commands)
            for command in commands:
                with self.subTest(command=command):
                    subprocess.run(shlex.split(command), cwd=root, check=True, capture_output=True, text=True)

    def test_secrets_inherit_fails(self) -> None:
        root = self.make_root()
        workflow = root / ".github" / "workflows" / "perf-test.yml"
        workflow.write_text(
            workflow.read_text(encoding="utf-8").replace(
                "    runs-on: ubuntu-latest", "    secrets: inherit\n    runs-on: ubuntu-latest"
            ),
            encoding="utf-8",
        )
        errors = POLICY.validate(root)
        self.assertTrue(any("secrets capability" in error for error in errors))

    def test_dot_and_bracket_secret_references_fail(self) -> None:
        for reference in (
            "${{ secrets.DEPLOY_TOKEN }}",
            "${{ secrets['DEPLOY_TOKEN'] }}",
            "${{ toJSON(secrets) }}",
        ):
            with self.subTest(reference=reference):
                errors = self.set_fork_ci_step(f"run: echo {reference}")
                self.assertTrue(any("secrets reference" in error for error in errors))

    def test_deployment_environment_fails(self) -> None:
        root = self.make_root()
        workflow = root / ".github" / "workflows" / "windows-smoke.yml"
        workflow.write_text(
            workflow.read_text(encoding="utf-8").replace(
                "    runs-on: ubuntu-latest", "    environment: production\n    runs-on: ubuntu-latest"
            ),
            encoding="utf-8",
        )
        errors = POLICY.validate(root)
        self.assertTrue(any("deployment environment" in error for error in errors))

    def test_aliased_write_permission_fails(self) -> None:
        root = self.make_root()
        workflow = root / ".github" / "workflows" / "fork-ci.yml"
        workflow.write_text(
            workflow.read_text(encoding="utf-8")
            .replace(
                "permissions: {contents: read}",
                "permissions: &fork_permissions {contents: write}",
            )
            .replace(
                "    runs-on: ubuntu-latest",
                "    permissions: *fork_permissions\n    runs-on: ubuntu-latest",
            ),
            encoding="utf-8",
        )
        errors = POLICY.validate(root)
        self.assertTrue(any("forbidden access 'write'" in error for error in errors))

    def test_nonstandard_runner_fails(self) -> None:
        root = self.make_root()
        workflow = root / ".github" / "workflows" / "windows-smoke.yml"
        workflow.write_text(
            workflow.read_text(encoding="utf-8").replace("ubuntu-latest", "self-hosted"), encoding="utf-8"
        )
        errors = POLICY.validate(root)
        self.assertTrue(any("nonstandard runner" in error for error in errors))

    def set_fork_ci_step(self, step: str) -> list[str]:
        root = self.make_root()
        workflow = root / ".github" / "workflows" / "fork-ci.yml"
        workflow.write_text(
            "name: Test\n"
            "on:\n"
            "  push:\n"
            "    branches: [main]\n"
            "  pull_request:\n"
            "    branches: [main]\n"
            "  workflow_dispatch:\n"
            "permissions: {contents: read}\n"
            "jobs:\n"
            "  test:\n"
            "    runs-on: ubuntu-latest\n"
            "    steps:\n"
            f"      - {step}\n",
            encoding="utf-8",
        )
        return POLICY.validate(root)

    def assert_publishing_step_rejected(self, step: str) -> None:
        errors = self.set_fork_ci_step(step)
        self.assertTrue(
            any("publishing, release, or deployment step" in error for error in errors),
            f"step was accepted: {step!r}; errors: {errors}",
        )

    def test_proved_publishing_command_bypasses_fail(self) -> None:
        for command in (
            "uv publish",
            "uv --directory . publish",
            "twine upload dist/*",
            "cargo publish",
            "gh release create v1",
            "gh --repo owner/repo release create v1",
        ):
            with self.subTest(command=command):
                self.assert_publishing_step_rejected(f"run: {command}")

    def test_publishing_command_aliases_fail(self) -> None:
        for command in (
            "python -m twine upload dist/*",
            "python -m twine --non-interactive upload dist/*",
            "python3 -m twine upload dist/*",
            "python3.12 -I -m twine --non-interactive upload dist/*",
            "npm publish",
            "pnpm publish",
            "yarn npm publish",
            "poetry publish",
            "hatch publish",
            "flit publish",
            "gem push package.gem",
            "dotnet nuget push package.nupkg",
            "cosign sign --key cosign.key image:tag",
            "docker push example/image:tag",
            "docker buildx build --push .",
            "docker buildx build --push=true .",
            "git push origin main",
            "helm push chart.tgz oci://registry.example.com",
        ):
            with self.subTest(command=command):
                self.assert_publishing_step_rejected(f"run: {command}")

    def test_release_and_deployment_command_aliases_fail(self) -> None:
        for command in (
            "gh release upload v1 dist/*",
            "vercel deploy --prod",
            "netlify deploy --prod",
            "firebase deploy",
            "wrangler deploy",
            "fly deploy",
            "railway up",
            "kubectl apply -f deployment.yml",
            "kubectl --namespace prod apply -f deploy.yml",
            "helm upgrade --install app ./chart",
        ):
            with self.subTest(command=command):
                self.assert_publishing_step_rejected(f"run: {command}")

    def test_shell_wrappers_chains_and_continuations_do_not_bypass_policy(self) -> None:
        for step in (
            "run: env uv publish",
            "run: uv build && twine upload dist/*",
            "run: |\n          uv \\\n            publish",
            "run: GH release create v1",
        ):
            with self.subTest(step=step):
                self.assert_publishing_step_rejected(step)

    def test_nested_shell_payloads_do_not_bypass_policy(self) -> None:
        for step in (
            'run: env -S "uv publish"',
            'run: sh -c "uv publish"',
            "run: bash -c 'uv publish'",
            'run: |\n          env -S "uv ' + "\\" + '\n            publish"',
            'run: |\n          sh -c "uv ' + "\\" + '\n            publish"',
            "run: |\n          bash -c 'uv " + "\\" + "\n            publish'",
        ):
            with self.subTest(step=step):
                self.assert_publishing_step_rejected(step)

    def test_dynamic_shell_indirection_cannot_hide_publishing(self) -> None:
        for step in (
            "run: |\n          cmd=uv\n          $cmd publish",
            'run: eval "uv publish"',
            'run: bash -c "$(printf uv) publish"',
            r"run: env -Suv\ publish",
            r"run: env --split-string=uv\ publish",
            "run: env $cmd publish",
            "run: sudo $cmd publish",
            'run: command eval "$payload"',
            "run: env source ./publish-command.sh",
            "run: source ./publish-command.sh",
            "run: . ./publish-command.sh",
            r"run: e\v\a\l 'uv publish'",
            'run: bash -c "`printf uv` publish"',
            'run: bash -c "<(printf uv) publish"',
            r"run: u\v pub\lish",
        ):
            with self.subTest(step=step):
                self.assert_publishing_step_rejected(step)

    def test_proved_release_action_bypass_fails(self) -> None:
        self.assert_publishing_step_rejected("uses: softprops/action-gh-release@v2")

    def test_publishing_release_and_deployment_actions_fail(self) -> None:
        for action in (
            "pypa/gh-action-pypi-publish@release/v1",
            "actions/deploy-pages@v4",
            "ncipollo/release-action@v1",
            "peaceiris/actions-gh-pages@v4",
            "azure/webapps-deploy@v3",
            "google-github-actions/deploy-cloudrun@v2",
            "JS-DevTools/npm-publish@v3",
            "cloudflare/wrangler-action@v3",
            "./.github/actions/deploy",
        ):
            with self.subTest(action=action):
                self.assert_publishing_step_rejected(f"uses: {action}")

    def test_only_reviewed_step_actions_are_allowed(self) -> None:
        for action in (
            "actions/checkout@v6",
            "astral-sh/setup-uv@v7",
            "actions/setup-python@v6",
            "actions/upload-artifact@v7",
        ):
            with self.subTest(action=action):
                self.assertEqual(self.set_fork_ci_step(f"uses: {action}"), [])

        for action in (
            "actions/checkout@v5",
            "docker/build-push-action@v6",
            "owner/unreviewed-action@v1",
        ):
            with self.subTest(action=action):
                self.assert_publishing_step_rejected(f"uses: {action}")

    def test_job_level_reusable_workflow_call_fails(self) -> None:
        root = self.make_root()
        workflow = root / ".github" / "workflows" / "fork-ci.yml"
        workflow.write_text(
            workflow.read_text(encoding="utf-8").replace(
                "    runs-on: ubuntu-latest\n    steps:\n      - run: true",
                "    uses: owner/repository/.github/workflows/deploy.yml@main",
            ),
            encoding="utf-8",
        )
        errors = POLICY.validate(root)
        self.assertTrue(any("reusable workflow call" in error for error in errors), errors)

    def test_ordinary_build_test_and_read_only_release_steps_are_allowed(self) -> None:
        for step in (
            "run: uv build",
            "run: npm run build",
            "run: cargo test",
            "run: gh release view v1",
            "run: gh --repo owner/repo release view v1",
            "run: kubectl --namespace dev get pods",
            "run: docker buildx build --push=false .",
            'run: |\n          args=(--scale tiny)\n          ./scripts/test.sh "${args[@]}"',
            "run: echo 'uv publish is forbidden'",
            "uses: actions/upload-artifact@v7",
        ):
            with self.subTest(step=step):
                self.assertEqual(self.set_fork_ci_step(step), [])


if __name__ == "__main__":
    unittest.main()
