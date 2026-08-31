#!/usr/bin/env python3
"""Enforce the maintained fork's intentionally small Actions surface."""

from __future__ import annotations

import copy
import re
import shlex
import sys
from pathlib import Path
from typing import Any

import yaml

EXPECTED_EVENTS = {
    "fork-ci.yml": {"push", "pull_request", "workflow_dispatch"},
    "fork-policy.yml": {"pull_request_target"},
    "perf-test.yml": {"workflow_dispatch"},
    "windows-smoke.yml": {"workflow_dispatch"},
}
EXPECTED_FORK_CI_TRIGGER = {
    "push": {"branches": ["main"]},
    "pull_request": {"branches": ["main"]},
    "workflow_dispatch": None,
}
FORBIDDEN_WORKFLOWS = {
    "deploy-docs.yml",
    "release-integration.yml",
    "release-tool.yml",
    "release.yml",
    "sign-images.yml",
    "star-history.yml",
    "test.yml",
}
STANDARD_RUNNERS = {"ubuntu-latest", "windows-latest", "macos-latest"}
ALLOWED_STEP_ACTIONS = {
    "actions/checkout@v6",
    "actions/setup-python@v6",
    "actions/upload-artifact@v7",
    "astral-sh/setup-uv@v7",
}
EXPECTED_FORK_POLICY_WORKFLOW = {
    "name": "Fork Workflow Policy",
    "on": {"pull_request_target": {"branches": ["main"]}},
    "permissions": {"contents": "read"},
    "jobs": {
        "policy": {
            "runs-on": "ubuntu-latest",
            "timeout-minutes": 5,
            "steps": [
                {
                    "name": "Checkout trusted policy",
                    "uses": "actions/checkout@v6",
                    "with": {
                        "ref": "${{ github.event.repository.default_branch }}",
                        "path": "trusted",
                        "persist-credentials": False,
                    },
                },
                {
                    "name": "Checkout immutable candidate",
                    "uses": "actions/checkout@v6",
                    "with": {
                        "repository": "${{ github.event.pull_request.head.repo.full_name }}",
                        "ref": "${{ github.event.pull_request.head.sha }}",
                        "path": "candidate",
                        "persist-credentials": False,
                    },
                },
                {
                    "name": "Set up trusted Python",
                    "uses": "actions/setup-python@v6",
                    "with": {"python-version-file": "trusted/.python-version"},
                },
                {"name": "Set up uv", "uses": "astral-sh/setup-uv@v7"},
                {
                    "name": "Test trusted policy validator",
                    "run": (
                        "uv run --directory trusted/hindsight-api-slim --frozen python "
                        "../tests/ci/test_validate_fork_workflows.py"
                    ),
                },
                {
                    "name": "Validate candidate with trusted policy",
                    "run": (
                        "uv run --directory trusted/hindsight-api-slim --frozen python "
                        "../scripts/ci/validate_fork_workflows.py ../../candidate"
                    ),
                },
            ],
        }
    },
}
FORBIDDEN_COMMAND_PREFIXES = {
    ("cargo", "publish"),
    ("cosign", "sign"),
    ("docker", "push"),
    ("dotnet", "nuget", "push"),
    ("firebase", "deploy"),
    ("flit", "publish"),
    ("fly", "deploy"),
    ("gem", "push"),
    ("git", "push"),
    ("hatch", "publish"),
    ("helm", "install"),
    ("helm", "push"),
    ("helm", "upgrade"),
    ("netlify", "deploy"),
    ("npm", "publish"),
    ("pnpm", "publish"),
    ("poetry", "publish"),
    ("python", "-m", "twine", "upload"),
    ("python3", "-m", "twine", "upload"),
    ("railway", "up"),
    ("twine", "upload"),
    ("uv", "publish"),
    ("vercel", "deploy"),
    ("wrangler", "deploy"),
    ("wrangler", "publish"),
    ("yarn", "npm", "publish"),
    ("yarn", "publish"),
}
FORBIDDEN_GH_RELEASE_COMMANDS = {"create", "delete", "edit", "upload"}
FORBIDDEN_KUBECTL_COMMANDS = {"apply", "create", "delete", "patch", "replace", "rollout", "set"}
SECRET_ACCESS = re.compile(r"(?<![A-Za-z0-9_])secrets\s*(?:\.|\[)", re.IGNORECASE)
ACTIONS_EXPRESSION = re.compile(r"\$\{\{(.*?)\}\}", re.DOTALL)
SECRET_IDENTIFIER = re.compile(r"(?<![A-Za-z0-9_])secrets(?![A-Za-z0-9_])", re.IGNORECASE)
SHELL_INTERPRETERS = {"bash", "dash", "ksh", "sh", "zsh"}
DYNAMIC_COMMAND = re.compile(r"^\$(?:[A-Za-z_][A-Za-z0-9_]*|\{[^}]+\})")
DYNAMIC_SHELL_SYNTAX = re.compile(r"\$\(|`|(?:<|>)\(")
DYNAMIC_COMMAND_WRAPPERS = {
    "builtin",
    "command",
    "env",
    "exec",
    "nice",
    "nohup",
    "stdbuf",
    "sudo",
    "time",
    "timeout",
    "xargs",
}
SHELL_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
SHELL_ARRAY_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*\+?=\(")
MAX_NESTED_PAYLOAD_DEPTH = 8


class GitHubActionsLoader(yaml.SafeLoader):
    """Parse Actions YAML 1.2 booleans instead of PyYAML's YAML 1.1 rules."""


GitHubActionsLoader.yaml_implicit_resolvers = copy.deepcopy(yaml.SafeLoader.yaml_implicit_resolvers)
for first_char, resolvers in GitHubActionsLoader.yaml_implicit_resolvers.items():
    GitHubActionsLoader.yaml_implicit_resolvers[first_char] = [
        (tag, pattern) for tag, pattern in resolvers if tag != "tag:yaml.org,2002:bool"
    ]
GitHubActionsLoader.add_implicit_resolver(
    "tag:yaml.org,2002:bool",
    re.compile(r"^(?:true|false)$", re.IGNORECASE),
    list("tTfF"),
)


def load_workflow(path: Path) -> dict[str, Any]:
    loader = GitHubActionsLoader(path.read_text(encoding="utf-8"))
    try:
        parsed = loader.get_single_data()
    finally:
        loader.dispose()
    if not isinstance(parsed, dict):
        raise ValueError("workflow root must be a mapping")
    return parsed


def workflow_events(workflow: dict[str, Any]) -> set[str]:
    events = workflow.get("on")
    if isinstance(events, str):
        return {events}
    if isinstance(events, list) and all(isinstance(event, str) for event in events):
        return set(events)
    if isinstance(events, dict) and all(isinstance(event, str) for event in events):
        return set(events)
    raise ValueError("top-level 'on' must be an event string, list, or mapping")


def permission_errors(scope: str, permissions: Any) -> list[str]:
    if permissions is None:
        return []
    if isinstance(permissions, str):
        return [] if permissions == "read-all" else [f"{scope}: permission shorthand {permissions!r} is forbidden"]
    if not isinstance(permissions, dict):
        return [f"{scope}: permissions must be a mapping or 'read-all'"]
    errors = []
    for permission, access in permissions.items():
        if access not in {"read", "none"}:
            errors.append(f"{scope}: permission {permission!r} has forbidden access {access!r}")
    return errors


def contains_secret_reference(value: str) -> bool:
    return bool(SECRET_ACCESS.search(value)) or any(
        SECRET_IDENTIFIER.search(expression) for expression in ACTIONS_EXPRESSION.findall(value)
    )


def sensitive_capability_errors(scope: str, value: Any, path: str = "workflow") -> list[str]:
    """Reject secret access and deployment environments anywhere in a workflow."""
    errors: list[str] = []
    if isinstance(value, dict):
        for key, child in value.items():
            child_path = f"{path}.{key}"
            if key == "secrets":
                errors.append(f"{scope}: secrets capability at {child_path} is forbidden")
            if key == "environment":
                errors.append(f"{scope}: deployment environment at {child_path} is forbidden")
            if isinstance(key, str) and contains_secret_reference(key):
                errors.append(f"{scope}: secrets reference at {child_path} is forbidden")
            errors.extend(sensitive_capability_errors(scope, child, child_path))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            errors.extend(sensitive_capability_errors(scope, child, f"{path}[{index}]"))
    elif isinstance(value, str) and contains_secret_reference(value):
        errors.append(f"{scope}: secrets reference at {path} is forbidden")
    return errors


def shell_segments(script: str) -> list[list[str]]:
    """Tokenize shell command segments while ignoring comments and quoted prose."""
    segments: list[list[str]] = []
    # Join explicit continuations first so a split `uv publish` is still one command.
    for line in script.replace("\\\n", " ").splitlines():
        lexer = shlex.shlex(line, posix=True, punctuation_chars=";&|")
        lexer.whitespace_split = True
        current: list[str] = []
        tokens = list(lexer)
        for token in tokens:
            if token and all(character in ";&|" for character in token):
                if current:
                    segments.append(current)
                    current = []
            else:
                current.append(token)
        if current:
            segments.append(current)
    return segments


def nested_command_payloads(tokens: list[str]) -> list[str]:
    """Return payloads interpreted as fresh command text by common wrappers."""
    payloads: list[str] = []
    normalized = [token.rsplit("/", 1)[-1].lower() for token in tokens]
    for index, command in enumerate(normalized):
        if command == "env":
            for option_index in range(index + 1, len(tokens)):
                option = tokens[option_index]
                if option in {"-S", "--split-string"} and option_index + 1 < len(tokens):
                    payloads.append(tokens[option_index + 1])
                elif option.startswith("-S") and option != "-S":
                    payloads.append(option[2:])
                elif option.startswith("--split-string="):
                    payloads.append(option.split("=", 1)[1])

        if command in SHELL_INTERPRETERS:
            for option_index in range(index + 1, len(tokens) - 1):
                option = tokens[option_index]
                if option.startswith("-") and "c" in option.lstrip("-"):
                    payloads.append(tokens[option_index + 1])
                    break
    return payloads


def script_is_forbidden(script: str, depth: int = 0) -> bool:
    # Substitutions generate command text at runtime, beyond what this static policy can prove safe.
    if DYNAMIC_SHELL_SYNTAX.search(script):
        return True
    return any(command_is_forbidden(segment, depth) for segment in shell_segments(script))


def command_is_forbidden(tokens: list[str], depth: int = 0) -> bool:
    normalized = [token.rsplit("/", 1)[-1].lower() for token in tokens]

    command_index = (
        None
        if tokens and SHELL_ARRAY_ASSIGNMENT.match(tokens[0])
        else next(
            (index for index, token in enumerate(tokens) if not SHELL_ASSIGNMENT.match(token)),
            None,
        )
    )
    if command_index is not None:
        command = normalized[command_index]
        # eval/source and variable command words can execute candidate-generated text that this
        # validator never sees. Reject the indirection rather than trying to emulate a shell.
        if command in {".", "eval", "source"} or DYNAMIC_COMMAND.match(tokens[command_index]):
            return True
        if command in DYNAMIC_COMMAND_WRAPPERS and any(
            DYNAMIC_COMMAND.match(token) or normalized[index] in {".", "eval", "source"}
            for index, token in enumerate(tokens[command_index + 1 :], command_index + 1)
        ):
            return True

    def contains_ordered(words: tuple[str, ...], haystack: list[str] = normalized) -> bool:
        """Match command structure without assuming options are contiguous."""
        next_word = 0
        for token in haystack:
            if token == words[next_word]:
                next_word += 1
                if next_word == len(words):
                    return True
        return False

    for prefix in FORBIDDEN_COMMAND_PREFIXES:
        if contains_ordered(prefix):
            return True

    if any(
        re.fullmatch(r"python(?:\d+(?:\.\d+)*)?", token)
        and contains_ordered(("-m", "twine", "upload"), normalized[index + 1 :])
        for index, token in enumerate(normalized)
    ):
        return True

    push_enabled = any(
        token == "--push" or token.startswith("--push=") and token != "--push=false" for token in normalized
    )
    if push_enabled and contains_ordered(("docker", "buildx", "build")):
        return True

    if any(contains_ordered(("gh", "release", command)) for command in FORBIDDEN_GH_RELEASE_COMMANDS):
        return True
    if any(contains_ordered(("kubectl", command)) for command in FORBIDDEN_KUBECTL_COMMANDS):
        return True

    payloads = nested_command_payloads(tokens)
    if payloads and depth >= MAX_NESTED_PAYLOAD_DEPTH:
        return True
    return any(script_is_forbidden(payload, depth + 1) for payload in payloads)


def step_policy_errors(scope: str, step: Any) -> list[str]:
    if not isinstance(step, dict):
        return [f"{scope}: step must be a mapping"]

    uses = step.get("uses")
    if isinstance(uses, str):
        # Step actions are executable code, so additions require explicit review.
        if uses.lower() not in ALLOWED_STEP_ACTIONS:
            return [f"{scope}: publishing, release, or deployment step {uses!r} is forbidden"]

    run = step.get("run")
    if isinstance(run, str):
        try:
            forbidden = script_is_forbidden(run)
        except ValueError:
            forbidden = True
        if forbidden:
            return [f"{scope}: publishing, release, or deployment step is forbidden"]
    return []


def validate(root: Path) -> list[str]:
    errors: list[str] = []
    if root.is_symlink():
        return ["candidate root: symbolic link is forbidden"]
    try:
        resolved_root = root.resolve(strict=True)
    except OSError as exc:
        return [f"candidate root: cannot resolve policy input: {exc}"]

    github_dir = root / ".github"
    workflow_dir = root / ".github" / "workflows"
    for label, path, boundary in (
        (".github", github_dir, resolved_root),
        (".github/workflows", workflow_dir, resolved_root),
    ):
        if path.is_symlink():
            errors.append(f"{label}: symbolic link is forbidden")
            continue
        if not path.is_dir():
            errors.append(f"{label}: policy input must be a directory")
            continue
        try:
            resolved = path.resolve(strict=True)
        except OSError as exc:
            errors.append(f"{label}: cannot resolve policy input: {exc}")
            continue
        if not resolved.is_relative_to(boundary):
            errors.append(f"{label}: resolved policy input escapes candidate root")
    if errors:
        return errors

    entries = list(workflow_dir.iterdir())
    for path in entries:
        if path.is_symlink():
            errors.append(f"{path.name}: symbolic link workflow entry is forbidden")
            continue
        try:
            resolved = path.resolve(strict=True)
        except OSError as exc:
            errors.append(f"{path.name}: cannot resolve policy input: {exc}")
            continue
        if not resolved.is_relative_to(workflow_dir.resolve(strict=True)):
            errors.append(f"{path.name}: resolved policy input escapes workflow directory")
    if errors:
        return errors

    actual = {path.name for path in entries if path.is_file() and path.suffix in {".yml", ".yaml"}}
    expected = set(EXPECTED_EVENTS)

    missing = expected - actual
    extra = actual - expected
    forbidden = actual & FORBIDDEN_WORKFLOWS
    if missing:
        errors.append(f"missing allowed workflows: {', '.join(sorted(missing))}")
    if extra:
        errors.append(f"unapproved workflow entrypoints: {', '.join(sorted(extra))}")
    if forbidden:
        errors.append(f"forbidden upstream workflows restored: {', '.join(sorted(forbidden))}")

    for name in sorted(actual & expected):
        path = workflow_dir / name
        try:
            workflow = load_workflow(path)
            events = workflow_events(workflow)
        except (ValueError, yaml.YAMLError) as exc:
            errors.append(f"{name}: invalid workflow YAML: {exc}")
            continue

        if events != EXPECTED_EVENTS[name]:
            errors.append(
                f"{name}: events {sorted(events)} do not match allowed events {sorted(EXPECTED_EVENTS[name])}"
            )
        if name == "fork-ci.yml" and workflow.get("on") != EXPECTED_FORK_CI_TRIGGER:
            errors.append(f"{name}: trigger configuration must exactly target main and allow manual dispatch")
        if name == "fork-policy.yml" and workflow != EXPECTED_FORK_POLICY_WORKFLOW:
            errors.append(f"{name}: trusted policy workflow must exactly match the reviewed configuration")
        errors.extend(sensitive_capability_errors(name, workflow))
        workflow_permissions = workflow.get("permissions")
        if workflow_permissions != {"contents": "read"}:
            errors.append(f"{name}: top-level permissions must be exactly {{'contents': 'read'}}")
        errors.extend(permission_errors(f"{name} workflow", workflow_permissions))

        jobs = workflow.get("jobs")
        if not isinstance(jobs, dict) or not jobs:
            errors.append(f"{name}: jobs must be a non-empty mapping")
        else:
            for job_name, job in jobs.items():
                if not isinstance(job, dict):
                    errors.append(f"{name} job {job_name!r}: job must be a mapping")
                    continue
                if "uses" in job:
                    errors.append(f"{name} job {job_name!r}: reusable workflow call is forbidden")
                    continue
                errors.extend(permission_errors(f"{name} job {job_name!r}", job.get("permissions")))
                runner = job.get("runs-on")
                if runner not in STANDARD_RUNNERS:
                    errors.append(f"{name} job {job_name!r}: nonstandard runner {runner!r}")
                steps = job.get("steps")
                if not isinstance(steps, list):
                    errors.append(f"{name} job {job_name!r}: steps must be a list")
                    continue
                for index, step in enumerate(steps, start=1):
                    errors.extend(step_policy_errors(f"{name} job {job_name!r} step {index}", step))

    return errors


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    if len(arguments) > 1:
        print("usage: validate_fork_workflows.py [candidate-root]", file=sys.stderr)
        return 2
    root = Path(arguments[0]) if arguments else Path(__file__).resolve().parents[2]
    errors = validate(root)
    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1
    print("Fork workflow policy OK: " + ", ".join(sorted(EXPECTED_EVENTS)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
