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
            elif name == "gate.yml":
                trigger = "on:\n  pull_request:\n    branches: [main]\n"
            elif name == "nightly.yml":
                trigger = "on:\n  schedule:\n    - cron: '53 6 * * *'\n  workflow_dispatch:\n"
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

    def test_byte_identical_perf_owner_rename_is_allowed(self) -> None:
        root = self.make_root()
        workflows = root / ".github" / "workflows"
        trusted_workflows = SCRIPT.parents[2] / ".github" / "workflows"
        owner = trusted_workflows / "perf-test.yml"
        if not owner.exists():
            owner = trusted_workflows / "fork-perf.yml"
        (workflows / "perf-test.yml").write_bytes(owner.read_bytes())
        self.assertEqual(POLICY.validate(root), [])
        (workflows / "perf-test.yml").rename(workflows / "fork-perf.yml")
        self.assertEqual(len(list(workflows.glob("*.yml"))), 6)
        self.assertEqual(POLICY.validate(root), [])

    def test_perf_rename_cannot_widen_trusted_policy(self) -> None:
        trusted_workflows = SCRIPT.parents[2] / ".github" / "workflows"
        owner = trusted_workflows / "perf-test.yml"
        if not owner.exists():
            owner = trusted_workflows / "fork-perf.yml"
        for case, expected_error in (
            ("altered", "must match the reviewed"),
            ("unsafe-alias", "top-level permissions"),
            ("duplicate", "unapproved workflow entrypoints"),
            ("unapproved", "unapproved workflow entrypoints"),
            ("symlink", "symbolic link"),
            ("unsafe-companion", "windows-smoke.yml: top-level permissions"),
        ):
            with self.subTest(case=case):
                root = self.make_root()
                workflows = root / ".github" / "workflows"
                original = workflows / "perf-test.yml"
                alias = workflows / "fork-perf.yml"
                original.write_bytes(owner.read_bytes())
                original.rename(alias)
                if case == "altered":
                    alias.write_bytes(alias.read_bytes() + b"\n# safe content change\n")
                elif case == "unsafe-alias":
                    alias.write_text(alias.read_text().replace("contents: read", "contents: write"), encoding="utf-8")
                elif case == "duplicate":
                    original.write_bytes(alias.read_bytes())
                elif case == "unapproved":
                    alias.rename(workflows / "other-perf.yml")
                elif case == "symlink":
                    target = root / "perf-owner.yml"
                    alias.rename(target)
                    alias.symlink_to(target)
                else:
                    companion = workflows / "windows-smoke.yml"
                    companion.write_text(
                        companion.read_text().replace("contents: read", "contents: write"), encoding="utf-8"
                    )
                errors = POLICY.validate(root)
                self.assertTrue(any(expected_error in error for error in errors), errors)

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

    def test_gate_branch_broadening_fails(self) -> None:
        root = self.make_root()
        workflow = root / ".github" / "workflows" / "gate.yml"
        workflow.write_text(
            workflow.read_text(encoding="utf-8").replace("branches: [main]", "branches: [main, develop]", 1),
            encoding="utf-8",
        )
        errors = POLICY.validate(root)
        self.assertTrue(any("gate.yml: trigger configuration" in error for error in errors))

    def test_gate_push_trigger_fails(self) -> None:
        root = self.make_root()
        workflow = root / ".github" / "workflows" / "gate.yml"
        workflow.write_text(
            workflow.read_text(encoding="utf-8").replace("on:\n", "on:\n  push:\n", 1),
            encoding="utf-8",
        )
        errors = POLICY.validate(root)
        self.assertTrue(any("gate.yml: events" in error for error in errors))

    def test_nightly_schedule_change_fails(self) -> None:
        root = self.make_root()
        workflow = root / ".github" / "workflows" / "nightly.yml"
        workflow.write_text(
            workflow.read_text(encoding="utf-8").replace("53 6 * * *", "*/5 * * * *", 1),
            encoding="utf-8",
        )
        errors = POLICY.validate(root)
        self.assertTrue(any("nightly.yml: trigger configuration" in error for error in errors))

    def test_repository_workflows_pass_policy(self) -> None:
        repo_root = SCRIPT.parents[2]
        self.assertEqual(len(list((repo_root / ".github" / "workflows").glob("*.yml"))), 6)
        self.assertEqual(POLICY.validate(repo_root), [])

    def test_every_permission_scope_rejects_write_at_workflow_and_job_levels(self) -> None:
        scopes = (
            "actions",
            "artifact-metadata",
            "attestations",
            "checks",
            "contents",
            "deployments",
            "discussions",
            "id-token",
            "issues",
            "models",
            "packages",
            "pages",
            "pull-requests",
            "security-events",
            "statuses",
            "future-scope",
        )
        for level in ("workflow", "job"):
            for scope in scopes:
                with self.subTest(level=level, scope=scope):
                    root = self.make_root()
                    path = root / ".github" / "workflows" / "fork-ci.yml"
                    workflow = POLICY.load_workflow(path)
                    owner = workflow if level == "workflow" else workflow["jobs"]["test"]
                    owner["permissions"] = {scope: "write"}
                    path.write_text(yaml.safe_dump(workflow, sort_keys=False), encoding="utf-8")
                    errors = POLICY.validate(root)
                    self.assertTrue(any("forbidden access 'write'" in error for error in errors), errors)

    def test_permissions_must_be_explicit_mappings_at_every_authored_level(self) -> None:
        for level in ("workflow", "job"):
            for value in (
                None,
                "write-all",
                "read-all",
                "none",
                [],
                42,
                True,
                {"contents": []},
                {"contents": None},
                {"contents": "${{ inputs.access }}"},
            ):
                with self.subTest(level=level, value=value):
                    root = self.make_root()
                    path = root / ".github" / "workflows" / "fork-ci.yml"
                    workflow = POLICY.load_workflow(path)
                    owner = workflow if level == "workflow" else workflow["jobs"]["test"]
                    owner["permissions"] = value
                    path.write_text(yaml.safe_dump(workflow, sort_keys=False), encoding="utf-8")
                    errors = POLICY.validate(root)
                    self.assertTrue(any("permission" in error for error in errors), errors)
        root = self.make_root()
        path = root / ".github" / "workflows" / "fork-ci.yml"
        workflow = POLICY.load_workflow(path)
        del workflow["permissions"]
        # An explicit safe job must not compensate for missing workflow authority.
        workflow["jobs"]["test"]["permissions"] = {"contents": "read"}
        path.write_text(yaml.safe_dump(workflow, sort_keys=False), encoding="utf-8")
        self.assertTrue(any("top-level permissions" in error for error in POLICY.validate(root)))

    def test_read_none_permissions_and_job_inheritance_pass(self) -> None:
        for permissions in (
            {},
            {"contents": "none"},
            {"contents": "read", "packages": "none"},
            {"issues": "read", "id-token": "none"},
        ):
            for job_permissions in ("omitted", {}, {"contents": "read"}, {"packages": "none"}):
                with self.subTest(permissions=permissions, job_permissions=job_permissions):
                    root = self.make_root()
                    path = root / ".github" / "workflows" / "fork-ci.yml"
                    workflow = POLICY.load_workflow(path)
                    workflow["permissions"] = permissions
                    if job_permissions != "omitted":
                        workflow["jobs"]["test"]["permissions"] = job_permissions
                    path.write_text(yaml.safe_dump(workflow, sort_keys=False), encoding="utf-8")
                    self.assertEqual(POLICY.validate(root), [])

    def test_secret_capability_grid_allows_only_literal_github_token(self) -> None:
        forbidden = (
            "${{ secrets.PYPI_API_TOKEN }}",
            "${{ secrets.NPM_TOKEN }}",
            "${{ secrets.DOCKER_PASSWORD }}",
            "${{ secrets.DEPLOY_TOKEN }}",
            "${{ secrets.GITHUB_TOKEN_EXTRA }}",
            "${{ secrets['GITHUB_TOKEN'] }}",
            "${{ secrets[inputs.name] }}",
            "${{ toJSON(secrets) }}",
            "${{ secrets }}",
            "${{ join(secrets.*, ',') }}",
            "${{ secrets.GITHUB_TOKEN || secrets.NPM_TOKEN }}",
            "${{ SECRETS.NPM_TOKEN }}",
        )
        for level in ("workflow", "job", "step"):
            for reference in (*forbidden, "${{ secrets.GITHUB_TOKEN }}"):
                with self.subTest(level=level, reference=reference):
                    root = self.make_root()
                    path = root / ".github" / "workflows" / "fork-ci.yml"
                    workflow = POLICY.load_workflow(path)
                    job = workflow["jobs"]["test"]
                    owner = workflow if level == "workflow" else job if level == "job" else job["steps"][0]
                    owner["env"] = {"TOKEN": reference}
                    path.write_text(yaml.safe_dump(workflow, sort_keys=False), encoding="utf-8")
                    errors = POLICY.validate(root)
                    if reference in forbidden:
                        self.assertTrue(any("secrets reference" in error for error in errors), errors)
                    else:
                        self.assertEqual(errors, [])
        for secrets in ("inherit", {"TOKEN": "${{ secrets.GITHUB_TOKEN }}"}, {}):
            with self.subTest(secrets=secrets):
                root = self.make_root()
                path = root / ".github" / "workflows" / "fork-ci.yml"
                workflow = POLICY.load_workflow(path)
                workflow["jobs"]["test"]["secrets"] = secrets
                path.write_text(yaml.safe_dump(workflow, sort_keys=False), encoding="utf-8")
                self.assertTrue(any("secrets capability" in error for error in POLICY.validate(root)))

    def test_read_only_github_token_does_not_excuse_write_permissions(self) -> None:
        root = self.make_root()
        path = root / ".github" / "workflows" / "fork-ci.yml"
        workflow = POLICY.load_workflow(path)
        workflow["jobs"]["test"]["env"] = {"TOKEN": "${{ secrets.GITHUB_TOKEN }}"}
        workflow["jobs"]["test"]["permissions"] = {"packages": "write"}
        path.write_text(yaml.safe_dump(workflow, sort_keys=False), encoding="utf-8")
        self.assertTrue(any("forbidden access 'write'" in error for error in POLICY.validate(root)))

    def test_all_publish_actions_reject_independent_of_ref_or_command_text(self) -> None:
        actions = (
            "pypa/gh-action-pypi-publish",
            "docker/login-action",
            "docker/build-push-action",
            "softprops/action-gh-release",
            "ncipollo/release-action",
            "actions/create-release",
            "actions/upload-release-asset",
            "JS-DevTools/npm-publish",
            "rust-lang/crates-io-auth-action",
            "goreleaser/goreleaser-action",
            "actions/deploy-pages",
            "peaceiris/actions-gh-pages",
            "azure/webapps-deploy",
            "google-github-actions/deploy-cloudrun",
            "cloudflare/wrangler-action",
        )
        for action in actions:
            for ref in ("v1", "main", "a" * 40):
                with self.subTest(action=action, ref=ref):
                    self.assert_publishing_step_rejected(f"uses: {action}@{ref}")
        for value in (None, [], {}, 42):
            with self.subTest(value=value):
                self.assert_publishing_step_rejected(
                    "uses: " + yaml.safe_dump(value, default_flow_style=True).splitlines()[0]
                )

    def test_all_reusable_workflows_reject_without_exceptions(self) -> None:
        for uses in (
            "./.github/workflows/gate.yml",
            "./.github/workflows/deploy.yml",
            "owner/repository/.github/workflows/gate.yml@main",
            "owner/repository/.github/workflows/deploy.yml@" + "a" * 40,
            "0xble/hindsight/.github/workflows/gate.yml@" + "a" * 40,
        ):
            with self.subTest(uses=uses):
                root = self.make_root()
                path = root / ".github" / "workflows" / "fork-ci.yml"
                workflow = POLICY.load_workflow(path)
                workflow["jobs"]["test"] = {"uses": uses}
                path.write_text(yaml.safe_dump(workflow, sort_keys=False), encoding="utf-8")
                self.assertTrue(any("reusable workflow call" in error for error in POLICY.validate(root)))
                self.assert_publishing_step_rejected(f"uses: {uses}")

    def test_capabilities_reject_even_when_shell_text_is_opaque(self) -> None:
        # The shell layer cannot inspect scripts loaded from the candidate tree.
        # Removing publishing authority must not depend on recognizing their text.
        for capability in ("write", "secret", "action", "reusable"):
            with self.subTest(capability=capability):
                root = self.make_root()
                path = root / ".github" / "workflows" / "fork-ci.yml"
                workflow = POLICY.load_workflow(path)
                job = workflow["jobs"]["test"]
                job["steps"] = [{"run": "./scripts/opaque.sh"}]
                path.write_text(yaml.safe_dump(workflow, sort_keys=False), encoding="utf-8")
                self.assertEqual(POLICY.validate(root), [])
                if capability == "write":
                    job["permissions"] = {"packages": "write"}
                elif capability == "secret":
                    job["env"] = {"TOKEN": "${{ secrets.NPM_TOKEN }}"}
                elif capability == "action":
                    job["steps"].append({"uses": "docker/login-action@v4"})
                else:
                    job["uses"] = "./.github/workflows/gate.yml"
                path.write_text(yaml.safe_dump(workflow, sort_keys=False), encoding="utf-8")
                self.assertNotEqual(POLICY.validate(root), [])

    def test_midword_hash_reviewer_reproductions_reject(self) -> None:
        for command in (
            "echo a#; npm $'publish'",
            'echo a#; V=publish; uv "$V"',
            "echo a#; uv publ$'i'sh",
            "echo a#; docker buildx build --output type=regi$'s'try .",
            "echo a#; gh release $'create' v1",
            "echo a#; kubectl $'apply' -f x",
        ):
            with self.subTest(command=command):
                self.assertTrue(POLICY.script_is_forbidden(command), command)
                self.assert_publishing_step_rejected("run: |\n          " + command)

    def test_hash_comments_start_only_at_word_boundaries(self) -> None:
        for command, expected in (
            ("echo a#; uv test", [["echo", "a#"], ["uv", "test"]]),
            ("echo a#x", [["echo", "a#x"]]),
            ("echo a # ignored; uv test", [["echo", "a"]]),
            ("echo a;# ignored; uv test", [["echo", "a"]]),
            ("echo 'a#'; uv test", [["echo", "a#"], ["uv", "test"]]),
            ("echo ''#x; uv test", [["echo", "#x"], ["uv", "test"]]),
            (r"echo \#x; uv test", [["echo", "#x"], ["uv", "test"]]),
        ):
            with self.subTest(command=command):
                self.assertEqual(POLICY.shell_segments(command), expected)
                self.assertFalse(POLICY.script_is_forbidden(command), command)

    def test_raw_dynamic_provenance_scans_past_midword_hash(self) -> None:
        for word in ("a#$V", "a#*", "a#?", "a#{b,c}", "a#`x`", r"a#\x", "a#$'x'", 'a#"$V"'):
            with self.subTest(word=word):
                self.assertTrue(POLICY.raw_word_is_dynamic(word), word)
        for word in ("a#literal", "'a#$V'", '"a#literal"'):
            with self.subTest(word=word):
                self.assertFalse(POLICY.raw_word_is_dynamic(word), word)

    def test_dollar_quote_grid_rejects_in_normalized_backstop(self) -> None:
        prefixes = (
            POLICY.FORBIDDEN_COMMAND_PREFIXES
            | {("gh", "release", verb) for verb in POLICY.FORBIDDEN_GH_RELEASE_COMMANDS}
            | {("kubectl", verb) for verb in POLICY.FORBIDDEN_KUBECTL_COMMANDS}
        )
        for prefix in sorted(prefixes):
            for quote in ("'", '"'):
                for position in range(len(prefix)):
                    word = prefix[position]
                    for replacement in (f"${quote}{word}{quote}", word[:1] + f"${quote}{word[1:]}{quote}"):
                        words = list(prefix)
                        words[position] = replacement
                        command = "echo a#; " + " ".join(words)
                        with self.subTest(command=command):
                            self.assertTrue(POLICY.normalized_publisher_text_is_forbidden(command), command)
                            self.assertTrue(POLICY.script_is_forbidden(command), command)
        for quote in ("'", '"'):
            for word in (f"type=regi${quote}stry{quote}", f"type=image,push=${quote}true{quote}"):
                command = "echo a#; docker buildx build --output " + word + " ."
                with self.subTest(command=command):
                    self.assertTrue(POLICY.normalized_publisher_text_is_forbidden(command), command)

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

    def test_publishing_step_shell_templates_fail(self) -> None:
        for shell in ("uv publish {0}", "bash -c 'uv publish' {0}", "gh release create v1 {0}"):
            with self.subTest(shell=shell):
                self.assert_publishing_step_rejected(f"run: echo safe\n        shell: {shell}")

    def test_publishing_default_shell_templates_fail(self) -> None:
        for scope in ("workflow", "job"):
            for overridden in (False, True):
                with self.subTest(scope=scope, overridden=overridden):
                    root = self.make_root()
                    workflow = root / ".github" / "workflows" / "fork-ci.yml"
                    candidate = POLICY.load_workflow(workflow)
                    job = candidate["jobs"]["test"]
                    owner = candidate if scope == "workflow" else job
                    owner["defaults"] = {"run": {"shell": "uv publish {0}"}}
                    job["steps"] = [{"run": "echo safe"}]
                    if overridden:
                        # Safe overrides must not hide unsafe authored defaults from the policy.
                        job["steps"][0]["shell"] = "bash"
                    workflow.write_text(yaml.safe_dump(candidate, sort_keys=False), encoding="utf-8")

                    errors = POLICY.validate(root)
                    self.assertTrue(
                        any(
                            "defaults.run.shell" in error and "publishing, release, or deployment" in error
                            for error in errors
                        ),
                        f"{scope} shell template was accepted: {errors}",
                    )

    def test_safe_shell_templates_are_allowed_at_every_scope(self) -> None:
        for scope in ("step", "workflow", "job"):
            for shell in ("bash", "pwsh", "bash --noprofile --norc -e -o pipefail {0}", "python {0}"):
                with self.subTest(scope=scope, shell=shell):
                    root = self.make_root()
                    workflow = root / ".github" / "workflows" / "fork-ci.yml"
                    candidate = POLICY.load_workflow(workflow)
                    job = candidate["jobs"]["test"]
                    job["steps"] = [{"run": "echo safe"}]
                    if scope == "step":
                        job["steps"][0]["shell"] = shell
                    else:
                        owner = candidate if scope == "workflow" else job
                        owner["defaults"] = {"run": {"shell": shell, "working-directory": "."}}
                    workflow.write_text(yaml.safe_dump(candidate, sort_keys=False), encoding="utf-8")
                    self.assertEqual(POLICY.validate(root), [])

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

    def test_dynamic_publisher_subcommands_fail_closed(self) -> None:
        for command in (
            'verb=publish; uv "$verb"',
            'uv "${VERB}"',
            'uv pub"${SUFFIX}"',
            'uv "$1"',
            'uv "${args[@]}"',
            "uv ${{ inputs.verb }}",
            'uv --directory "$PROJECT" "$VERB"',
            'uv --config-file "$CONFIG" "$VERB"',
            "uv --${OPTION} build",
            'cargo "$VERB"',
            'cargo +stable "$VERB"',
            'npm "$VERB"',
            'pnpm "$VERB"',
            'yarn npm "$VERB"',
            'dotnet nuget "$VERB"',
            'docker "$VERB" image',
            'docker buildx "$VERB" .',
            'git -C "$PROJECT" "$VERB" origin main',
            'gh --repo "$REPO" release "$VERB" v1',
            'gh "$GROUP" create v1',
            'kubectl --namespace "$NAMESPACE" "$VERB" -f deploy.yml',
            'python3.12 -I -m twine "$VERB" dist/*',
            'python -m "$MODULE" upload dist/*',
            'twine --repository-url "$REGISTRY" "$VERB" dist/*',
            'cosign "$VERB" image',
            'helm "$VERB" chart',
            'env uv "$VERB"',
            "bash -c 'uv \"$VERB\"'",
        ):
            with self.subTest(command=command):
                self.assert_publishing_step_rejected(f"run: {command}")

    def test_dynamic_wrapper_command_words_fail_closed(self) -> None:
        for command in (
            'env "$COMMAND" publish',
            'env -u NAME "$COMMAND" publish',
            'env --chdir "$PROJECT" "$COMMAND" publish',
            'command "$COMMAND" publish',
            'timeout 10 "$COMMAND" publish',
            'sudo -u runner "$COMMAND" publish',
            'sudo -p prompt "$COMMAND" publish',
            'xargs -I {} "$COMMAND" publish',
            'env --argv0 name "$COMMAND" publish',
            'uv --default-index https://example.com "$COMMAND"',
            'env --unreviewed-option name "$COMMAND" publish',
            'uv --unreviewed-option value "$COMMAND"',
        ):
            with self.subTest(command=command):
                self.assert_publishing_step_rejected(f"run: {command}")

    def test_data_arguments_are_not_dynamic_publisher_subcommands(self) -> None:
        for command in (
            'uv --directory "$PROJECT" run pytest "$TESTS"',
            'uv --config-file "$CONFIG" build',
            'uv run pytest "$TESTS"',
            'cargo test "$FILTER"',
            'cargo +stable test "$FILTER"',
            'npm run build -- "$TARGET"',
            'git -C "$PROJECT" status',
            'gh --repo "$REPO" release view "$TAG"',
            'kubectl --namespace "$NAMESPACE" get pods',
            'python "$TEST_SCRIPT"',
            'python -m pytest "$TESTS"',
            'env TESTS="$TESTS" pytest "$TESTS"',
            'env -u NAME pytest "$TESTS"',
            'env --chdir "$PROJECT" pytest "$TESTS"',
            'command pytest "$TESTS"',
            'timeout 10 pytest "$TESTS"',
            'sudo -u runner pytest "$TESTS"',
            'env uv --directory "$PROJECT" run pytest "$TESTS"',
            'uv --default-index "$INDEX" run pytest "$TESTS"',
            'env --argv0 name pytest "$TESTS"',
            'env --ignore-environment pytest "$TESTS"',
            'xargs -I {} pytest "$TESTS"',
            'sudo -p prompt pytest "$TESTS"',
            'uv --offline run pytest "$TESTS"',
            'uv -p"$PYTHON" run pytest "$TESTS"',
            'git -C"$PROJECT" status',
            'env -C"$PROJECT" pytest "$TESTS"',
            "bash -c 'uv run pytest \"$TESTS\"'",
            "echo 'uv $VERB is not literal prose to execute'",
        ):
            with self.subTest(command=command):
                self.assertEqual(self.set_fork_ci_step(f"run: {command}"), [])

    def test_buildx_publishing_exporters_fail_closed(self) -> None:
        for command in (
            "docker buildx build --output type=registry .",
            "docker buildx build --output=type=registry .",
            "docker buildx build -o type=registry .",
            "docker buildx build -otype=registry .",
            "docker buildx build -o=type=registry .",
            "docker buildx build --output type=registry,push=false .",
            "docker buildx build --output type=image,push=true .",
            "docker buildx build --output 'type=image, push =true' .",
            "docker buildx build --output ' type =registry,name=example/image' .",
            "docker buildx build --output=type=image,push=1 .",
            "docker buildx build -o type=image,push=True .",
            "docker buildx build -otype=image,push=t .",
            "docker buildx build --push=false -o type=image,push=true .",
            "docker buildx build -o type=local,dest=out -o type=registry .",
            "docker buildx build --output 'type=image,\"name=one,two\",push=true' .",
            "docker buildx build --output '\"type=registry\",name=example/image' .",
            "docker buildx build --output type=image,push-by-digest=true .",
            'exporter=type=registry; docker buildx build --output "$exporter" .',
            'docker buildx build --output="${EXPORTER}" .',
            'docker buildx build -o "$EXPORTER" .',
            "docker buildx build -o${EXPORTER} .",
            "docker buildx build --output type=${TYPE},dest=out .",
            "docker buildx build --output type=image,push=${PUSH:-false} .",
            'docker buildx build --output type=image,"${KEY}"=true .',
            'docker buildx build --output type=local,dest="$DEST" .',
            "env docker buildx build --output type=registry .",
            "bash -c 'docker buildx build -o type=registry .'",
        ):
            with self.subTest(command=command):
                self.assert_publishing_step_rejected(f"run: {command}")

    def test_local_buildx_exporters_and_data_arguments_are_allowed(self) -> None:
        for command in (
            "docker buildx build --output type=local,dest=out .",
            "docker buildx build --output=type=tar,dest=out.tar .",
            "docker buildx build -o type=docker .",
            "docker buildx build -otype=oci,dest=out.tar .",
            "docker buildx build -o=type=image,push=false .",
            "docker buildx build --output type=image,push=0 .",
            "docker buildx build --output type=image,push=FALSE .",
            "docker buildx build --output type=image,push=f .",
            "docker buildx build --output type=image .",
            "docker buildx build --output ./out .",
            "docker buildx build -o - .",
            "docker buildx build -o type=local,dest=out -o type=tar,dest=out.tar .",
            'docker buildx build -t "$IMAGE" --build-arg VALUE="$VALUE" --output type=local,dest=out "$CONTEXT"',
            'env docker buildx build --output type=local,dest=out "$CONTEXT"',
        ):
            with self.subTest(command=command):
                self.assertEqual(self.set_fork_ci_step(f"run: {command}"), [])

    def test_review_shell_expansion_reproductions_are_rejected_at_workflow_boundary(self) -> None:
        for command in (
            "uv {publish,}",
            "uv publis{h,}",
            "npm {publish,}",
            "cargo {publish,x}",
            r"uv pub\lish",
            "uv ~/x",
            "uv `printf publish`",
            "docker buildx build --output {type=registry,} .",
            "docker buildx build --output type=regis{try,} .",
            'docker buildx build "-o"type=regis{try,} .',
            'docker buildx build "--output="type=regis{try,} .',
            "docker buildx build   -o~/out .",
            "env u{v,} test",
            "command u* test",
            r"sudo u\v test",
            "~/bin/uv test",
        ):
            with self.subTest(command=command):
                self.assert_publishing_step_rejected(f"run: {command}")

    def test_escaped_newlines_cannot_hide_command_words_or_exporters(self) -> None:
        for command in (
            "u\\\nv publish",
            "uv pub\\\nlish",
            "uv te\\\nst",
            "u\\\nv {publish,}",
            "env u\\\nv test",
            "docker buildx build --output type=regis\\\ntry .",
            "docker buildx build -otype=regis\\\ntry .",
        ):
            with self.subTest(command=command):
                self.assertTrue(POLICY.script_is_forbidden(command), command)
        for command in (
            "uv \\\n  test",
            "uv run \\\n  pytest",
            "docker buildx build \\\n  --output type=local,dest=out .",
        ):
            with self.subTest(command=command):
                self.assertTrue(POLICY.script_is_forbidden(command), command)

    def test_quoted_line_continuation_reviewer_repros_fail(self) -> None:
        for command in (
            '"u\\\nv" publish',
            'uv "pub\\\nlish"',
            'docker buildx build -o "type=regis\\\ntry" .',
        ):
            with self.subTest(command=command):
                self.assertTrue(POLICY.script_is_forbidden(command), command)
                self.assert_publishing_step_rejected("run: |\n          " + command.replace("\n", "\n          "))

    def test_all_line_continuations_fail_in_every_shell_scope(self) -> None:
        for quote in ('"', "'", ""):
            for newline in ("\n", "\r\n"):
                command = f"echo {quote}safe\\{newline} text{quote}"
                with self.subTest(quote=quote, newline=newline):
                    self.assertTrue(POLICY.script_is_forbidden(command), command)
                    for scope in ("run", "shell", "workflow-default", "job-default"):
                        root = self.make_root()
                        path = root / ".github" / "workflows" / "fork-ci.yml"
                        workflow = POLICY.load_workflow(path)
                        job = workflow["jobs"]["test"]
                        if scope in {"run", "shell"}:
                            job["steps"] = [{"run": "echo safe", scope: command}]
                        else:
                            owner = workflow if scope == "workflow-default" else job
                            owner["defaults"] = {"run": {"shell": command}}
                        path.write_text(yaml.safe_dump(workflow, sort_keys=False), encoding="utf-8")
                        with self.subTest(scope=scope):
                            self.assertTrue(any("line continuation" in error for error in POLICY.validate(root)))

    def test_normalized_publisher_token_grid_fails_closed(self) -> None:
        prefixes = (
            POLICY.FORBIDDEN_COMMAND_PREFIXES
            | {("gh", "release", verb) for verb in POLICY.FORBIDDEN_GH_RELEASE_COMMANDS}
            | {("kubectl", verb) for verb in POLICY.FORBIDDEN_KUBECTL_COMMANDS}
        )
        disguises = (
            lambda word: '"' + word + '"',
            lambda word: "'" + word + "'",
            lambda word: word[:1] + '""' + word[1:],
            lambda word: "\\".join(word),
            lambda word: "{" + word + "}",
            lambda word: "[" + word + "]",
            lambda word: "*" + word + "?",
        )
        for prefix in sorted(prefixes):
            for disguise in disguises:
                command = " \t ".join(disguise(word) for word in prefix)
                with self.subTest(command=command):
                    # Exercise the coarse backstop itself: precise checks cannot
                    # accidentally conceal a normalization regression.
                    self.assertTrue(POLICY.normalized_publisher_text_is_forbidden(command), command)
                    self.assertTrue(POLICY.script_is_forbidden(command), command)
        tools = {prefix[0] for prefix in prefixes} | {"buildx"}
        for tool in sorted(tools):
            for verb in ("publish", "push"):
                for command in (f"'{tool}' \"{verb}\"", f"'{verb}' \"{tool}\""):
                    with self.subTest(command=command):
                        self.assertTrue(POLICY.normalized_publisher_text_is_forbidden(command), command)

    def test_normalized_buildx_and_exporter_grid_fails_closed(self) -> None:
        for command in (
            '"buildx" build "--push" .',
            "bui'ldx' build --pu'sh'=true .",
            'docker buildx build -o "ty\\pe=regis\\try" .',
            "docker buildx build -o '{type}=[registry]' .",
            'docker buildx build -o "type=image,push=tr\\ue" .',
            'echo "type=registry"',
            '# uv "publish"',
            "echo 'uv publish is forbidden'",
            "echo 'docker buildx build --output type=registry is forbidden'",
            'buildx build --push=false "--pu\\sh"=true .',
        ):
            with self.subTest(command=command):
                self.assertTrue(POLICY.normalized_publisher_text_is_forbidden(command), command)
                self.assertTrue(POLICY.script_is_forbidden(command), command)

    def test_normalized_safe_build_test_and_local_export_controls(self) -> None:
        for command in (
            "uv run pytest\necho done",
            "npm run build",
            "docker buildx build --push=false --output type=local,dest=out .",
            "buildx build --output type=image,push=false .",
            'echo "publishable pushdown uvx"',
        ):
            with self.subTest(command=command):
                self.assertFalse(POLICY.normalized_publisher_text_is_forbidden(command), command)
                self.assertFalse(POLICY.script_is_forbidden(command), command)

    def test_shell_structure_does_not_hide_expanded_executable_words(self) -> None:
        for command in (
            "(uv {publish,})",
            "(u{v,} publish)",
            "(uv publish)",
            "if u{v,} test; then echo ok; fi",
            "while u{v,} test; do echo ok; done",
            "until u{v,} test; do echo ok; done",
            "! u{v,} test",
            "if true; then u{v,} test; fi",
            "for item in items; do u{v,} test; done",
            "if false; then echo ok; else u{v,} test; fi",
        ):
            with self.subTest(command=command):
                self.assertTrue(POLICY.script_is_forbidden(command), command)
        for command in (
            "(uv test)",
            'if [ "$VALUE" != all ]; then uv test; fi',
            "while uv test; do echo ok; done",
            "! uv test",
            "echo '(uv test)'",
        ):
            with self.subTest(command=command):
                self.assertFalse(POLICY.script_is_forbidden(command), command)

    def test_unquoted_expansions_fail_closed_across_publisher_paths(self) -> None:
        prefixes = POLICY.FORBIDDEN_COMMAND_PREFIXES | {
            ("gh", "release", "create"),
            ("kubectl", "apply"),
            ("docker", "buildx", "build"),
            ("docker", "builder", "build"),
            ("buildx", "build"),
        }
        expansions = (
            "{word}*",
            "{word}?",
            "[{word}]",
            "{{{word},}}",
            "{word}{{x,}}",
            "{{{word},x}}",
            "{word}}}",
            "~/x",
            "$WORD",
            "`printf word`",
            "<(printf word)",
            ">(printf word)",
        )
        for prefix in sorted(prefixes):
            for position in range(len(prefix)):
                if prefix[position] == "-m":
                    continue  # Interpreter option, not a command/module/verb word.
                for template in expansions:
                    words = list(prefix)
                    word = words[position]
                    # An identity escape still must fail closed, even for safe verbs.
                    replacement = word[:1] + "\\" + word[1:]
                    for value in (template.format(word=word), replacement):
                        words[position] = value
                        command = " ".join(words)
                        with self.subTest(command=command):
                            self.assertTrue(POLICY.script_is_forbidden(command), command)

    def test_safe_verbs_with_unquoted_expansions_are_not_provably_safe(self) -> None:
        for tool in (
            "uv",
            "twine",
            "npm",
            "pnpm",
            "yarn",
            "cargo",
            "gh release",
            "docker buildx",
            "poetry",
            "hatch",
            "flit",
        ):
            for word in ("tes{t,}", r"te\st", "~/x", "test*", "test?", "[t]est", "test}"):
                command = f"{tool} {word}"
                with self.subTest(command=command):
                    self.assertTrue(POLICY.script_is_forbidden(command), command)

    def test_exporter_expansions_fail_closed_for_all_output_spellings(self) -> None:
        for tool in (
            "docker buildx build",
            "docker buildx b",
            "docker build",
            "docker builder build",
            "buildx build",
            "buildx b",
        ):
            for option in ("--output ", "--output=", "-o ", "-o", "-o="):
                for value in (
                    "{type=registry,}",
                    "type=regis{try,}",
                    "type=local,dest={out,x}",
                    "type=local,dest=out*",
                    "type=local,dest=out?",
                    "type=local,dest=[o]ut",
                    "type=local,dest=out}",
                    "~/out",
                    r"type=local,dest=o\ut",
                    "$OUTPUT",
                    "`printf type=registry`",
                    "<(printf type=registry)",
                ):
                    command = f"{tool} {option}{value} ."
                    with self.subTest(command=command):
                        self.assertTrue(POLICY.script_is_forbidden(command), command)

    def test_quote_provenance_and_literal_local_exporters_are_preserved(self) -> None:
        for command in (
            "uv 'test'",
            'uv "test"',
            "uv te'st'",
            "'uv' test",
            '"uv" test',
            "uv 'tes{t,}'",
            'uv "test*"',
            "uv '[t]est'",
            "uv '~/x'",
            r"uv 'te\st'",
            "uv test # {this is a comment}",
            "uv test; npm test",
            "uv test&&npm test",
            'echo "{prose}";uv test',
            'docker buildx build --output "type=docker" .',
            "docker buildx build --output type=local,dest=out .",
            "docker buildx build -o out .",
            "docker buildx build --output 'type=local,dest={out,x}' .",
            'docker buildx build --output="type=local,dest=out*" .',
            "docker buildx build -o'type=local,dest=[o]ut' .",
        ):
            with self.subTest(command=command):
                self.assertFalse(POLICY.script_is_forbidden(command), command)
        for command in ("'uv' 'publish'", "uv pub'li'sh", "docker buildx build -o'type=registry' ."):
            with self.subTest(command=command):
                self.assertTrue(POLICY.script_is_forbidden(command), command)

    def test_publisher_subcommand_globs_fail_closed(self) -> None:
        for command in ("uv pub*", "npm publis?", "cargo [p]ublish"):
            with self.subTest(command=command):
                self.assert_publishing_step_rejected(f"run: {command}")

    def test_all_known_publisher_paths_reject_variable_verbs(self) -> None:
        for prefix in sorted(POLICY.FORBIDDEN_COMMAND_PREFIXES):
            command = " ".join((*prefix[:-1], '"$VERB"'))
            with self.subTest(command=command):
                self.assert_publishing_step_rejected(f"run: {command}")

    def test_buildx_exporters_cannot_bypass_policy_via_build_alias(self) -> None:
        self.assert_publishing_step_rejected("run: docker buildx b -o type=registry .")

    def test_publisher_policy_covers_shell_templates_at_all_scopes(self) -> None:
        for command in ('uv "$VERB" {0}', "docker buildx build -o type=registry {0}"):
            for scope in ("step", "workflow", "job"):
                with self.subTest(scope=scope, command=command):
                    root = self.make_root()
                    workflow = root / ".github" / "workflows" / "fork-ci.yml"
                    candidate = POLICY.load_workflow(workflow)
                    job = candidate["jobs"]["test"]
                    job["steps"] = [{"run": "echo safe"}]
                    if scope == "step":
                        job["steps"][0]["shell"] = command
                    else:
                        owner = candidate if scope == "workflow" else job
                        owner["defaults"] = {"run": {"shell": command}}
                    workflow.write_text(yaml.safe_dump(candidate, sort_keys=False), encoding="utf-8")
                    self.assertTrue(
                        any("publishing, release, or deployment step" in error for error in POLICY.validate(root))
                    )

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
            "uses: actions/upload-artifact@v7",
        ):
            with self.subTest(step=step):
                self.assertEqual(self.set_fork_ci_step(step), [])


if __name__ == "__main__":
    unittest.main()
