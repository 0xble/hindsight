#!/usr/bin/env python3
"""Enforce the maintained fork's intentionally small Actions surface."""

from __future__ import annotations

import copy
import csv
import hashlib
import re
import shlex
import sys
from collections import deque
from io import StringIO
from pathlib import Path
from typing import Any

import yaml

EXPECTED_EVENTS = {
    "fork-ci.yml": {"push", "pull_request", "workflow_dispatch"},
    "fork-policy.yml": {"pull_request_target"},
    "gate.yml": {"pull_request"},
    "nightly.yml": {"schedule", "workflow_dispatch"},
    "perf-test.yml": {"workflow_dispatch"},
    "windows-smoke.yml": {"workflow_dispatch"},
}
# Permit the reviewed fork owner to take a name distinct from upstream's perf
# workflow. Pin trusted bytes here, never to a reference chosen by the candidate.
# SHA256 of .github/workflows/perf-test.yml at c7c4be93ec7411feaf448233b5d7d2b12c3f60c9.
RENAMED_PERF_SHA256 = "61e1bdd9772c2000b497f385bb4f9a10f0e7975db10d20e80a77ce2e8d1630b1"
EXPECTED_FORK_CI_TRIGGER = {
    "push": {"branches": ["main"]},
    "pull_request": {"branches": ["main"]},
    "workflow_dispatch": None,
}
# Repository CI contract: the exact-SHA gate runs only for PRs into main, and the
# nightly only on its fixed schedule or by hand. Neither may widen its trigger.
EXPECTED_GATE_TRIGGER = {
    "pull_request": {
        "branches": ["main"],
        "types": ["opened", "synchronize", "reopened", "labeled", "unlabeled"],
    }
}
# Keep the existing trigger valid while the default-branch trusted policy lands.
# A later queue rollout may opt into this exact draft-until-ready event set; no
# push, dispatch, path filters, other branches, or additional PR types are allowed.
QUEUE_GATE_TRIGGER = {
    "pull_request": {
        "branches": ["main"],
        "types": ["opened", "synchronize", "reopened", "ready_for_review"],
    }
}
REQUIRED_MERGIFY_CONDITIONS = (
    "base = main",
    "-draft",
    "check-success = qualification",
    "check-success = policy",
    "author = 0xble",
    "head-repo-full-name = 0xble/hindsight",
)
EXPECTED_NIGHTLY_TRIGGER = {"schedule": [{"cron": "53 6 * * *"}], "workflow_dispatch": None}
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
READ_ONLY_TOKEN_REFERENCE = re.compile(r"(?<![A-Za-z0-9_])secrets\s*\.\s*GITHUB_TOKEN(?![A-Za-z0-9_]|\s*[.\[])")
SECRET_IDENTIFIER = re.compile(r"(?<![A-Za-z0-9_])secrets(?![A-Za-z0-9_])", re.IGNORECASE)
SHELL_INTERPRETERS = {"bash", "dash", "ksh", "sh", "zsh"}
COMMAND_BOOLEAN_OPTIONS = {
    "--debug",
    "--foreground",
    "--frozen",
    "--help",
    "--ignore-environment",
    "--locked",
    "--no-cache",
    "--no-config",
    "--no-progress",
    "--offline",
    "--preserve-status",
    "--quiet",
    "--verbose",
    "--version",
    "-D",
    "-V",
    "-h",
    "-i",
    "-n",
    "-p",
    "-q",
    "-v",
    "-vv",
    "-vvv",
}
DYNAMIC_SHELL_SYNTAX = re.compile(r"\$\(|`|(?:<|>)\(")
SHELL_COMMAND_PREFIXES = {"!", "if", "then", "elif", "else", "while", "until", "do"}
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
    # Missing workflow authority and any authored shorthand fail closed. Jobs
    # without this key inherit the explicit workflow block; callers distinguish
    # omission from an authored null before invoking this check.
    if not isinstance(permissions, dict):
        return [f"{scope}: permissions must be an explicit read/none-only mapping"]
    errors = []
    for permission, access in permissions.items():
        if access not in ("read", "none"):
            errors.append(f"{scope}: permission {permission!r} has forbidden access {access!r}")
    return errors


def contains_secret_reference(value: str) -> bool:
    # Only the literal read-only job token is approved. Leave dynamic indexing,
    # whole-context access (including toJSON), and every other secret visible.
    return bool(SECRET_IDENTIFIER.search(READ_ONLY_TOKEN_REFERENCE.sub("", value)))


def sensitive_capability_errors(scope: str, value: Any, path: str = "workflow") -> list[str]:
    """Reject publishing credentials and deployment environments at every scope."""
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


class ShellWord(str):
    """Decoded shlex word with its authored expansion/quoting evidence intact."""

    dynamic: bool
    raw: str

    def __new__(cls, value: str, raw: str) -> ShellWord:
        word = super().__new__(cls, value)
        word.raw = raw.lstrip()
        word.dynamic = raw_word_is_dynamic(raw)
        return word


def raw_word_is_dynamic(raw: str) -> bool:
    """Fail closed on shell expansion syntax, not metacharacters in literal quotes."""
    quote: str | None = None
    index = 0
    raw = raw.lstrip()
    while index < len(raw):
        character = raw[index]
        if quote == "'":
            if character == "'":
                quote = None
        elif quote == '"':
            if character == '"':
                quote = None
            elif character == "\\":
                index += 1  # Quoted escape: shlex already decoded the literal word.
            elif character in "$`":
                return True  # Double quotes do not suppress shell substitutions.
        elif character in "'\"":
            quote = character
        elif character in "$`*?[{}\\" or character == "~" and index == 0:
            return True
        elif raw[index : index + 2] in {"<(", ">("}:
            return True
        index += 1
    return False


def command_word_is_dynamic(word: str) -> bool:
    # Plain-string callers have no quotation evidence and must also fail closed.
    return word.dynamic if isinstance(word, ShellWord) else raw_word_is_dynamic(word)


class PolicyShellLexer(shlex.shlex):
    """Reuse shlex's word boundaries while retaining the raw source of each word."""

    _pushback_chars: deque[str]

    def __init__(self, source: str) -> None:
        # shlex incorrectly starts comments at mid-word '#'. Mask only comments
        # that begin a word, preserving offsets for the raw provenance scan.
        characters = list(source)
        quote: str | None = None
        word_started = False
        index = 0
        while index < len(source):
            character = source[index]
            if quote == "'":
                if character == "'":
                    quote = None
            elif character == "\\":
                word_started = True
                index += 1
            elif quote == '"':
                if character == '"':
                    quote = None
            elif character in "'\"":
                quote = character
                word_started = True
            elif character.isspace() or character in ";&|()":
                word_started = False
            elif character == "#" and not word_started:
                while index < len(source) and source[index] != "\n":
                    characters[index] = " "
                    index += 1
                continue
            else:
                word_started = True
            index += 1
        source = "".join(characters)
        self.source_stream = StringIO(source)
        super().__init__(self.source_stream, posix=True, punctuation_chars=";&|()")
        self.source_text = source
        self.whitespace_split = True
        self.commenters = ""

    def read_token(self) -> str | None:
        # shlex reads one character ahead at punctuation boundaries. Account for
        # its pending characters so adjacent `test;uv` words keep exact provenance.
        start = self.source_stream.tell() - len(self._pushback_chars)
        token = super().read_token()
        end = self.source_stream.tell() - len(self._pushback_chars)
        # shlex retains the LF from an escaped newline; a shell removes it. Keep
        # the raw escape for authority checks even after normalizing the word.
        return None if token is None else ShellWord(token.replace("\n", ""), self.source_text[start:end])


def shell_segments(script: str) -> list[list[str]]:
    """Tokenize shell command segments while ignoring comments and quoted prose."""
    segments: list[list[str]] = []
    # Keep escaped newlines in logical lines so shlex sees the actual word,
    # while raw provenance still exposes `u\\\nv` and `pub\\\nlish` escapes.
    lines: list[str] = []
    pending = ""
    for line in script.splitlines(keepends=True):
        pending += line
        if not line.endswith("\\\n"):
            lines.append(pending)
            pending = ""
    if pending:
        lines.append(pending)
    for line in lines:
        lexer = PolicyShellLexer(line)
        current: list[str] = []
        tokens = list(lexer)
        for token in tokens:
            if not token:
                continue  # A continuation between words does not create a word.
            raw = token.raw if isinstance(token, ShellWord) else token
            if all(character in ";&|()" for character in token) and raw.strip() == token:
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


def normalized_publisher_text_is_forbidden(script: str) -> bool:
    """Best-effort text backstop, deliberately independent of shell parsing."""
    # Quote/escape provenance has repeatedly exposed parser mismatches. Treat even
    # comments and quoted data as suspect when normalization exposes a publisher;
    # this is a workflow policy, not an attempt to interpret arbitrary shell code.
    normalized = " ".join(script.translate(str.maketrans("", "", "'\"`\\{}[]*?$")).lower().split())
    prefixes = (
        FORBIDDEN_COMMAND_PREFIXES
        | {("gh", "release", verb) for verb in FORBIDDEN_GH_RELEASE_COMMANDS}
        | {("kubectl", verb) for verb in FORBIDDEN_KUBECTL_COMMANDS}
    )
    for prefix in prefixes:
        pattern = r"(?<![\w.-])" + r"\s+".join(re.escape(word) for word in prefix) + r"(?![\w.-])"
        if re.search(pattern, normalized):
            return True
    tools = {prefix[0] for prefix in prefixes} | {"buildx"}
    for tool in tools:
        pattern = rf"(?<![\w.-])(?:{tool}\s+(?:publish|push)|(?:publish|push)\s+{tool})(?![\w.-])"
        if re.search(pattern, normalized):
            return True
    if re.search(r"(?<![\w.-])(?:type\s*=\s*registry|push(?:-by-digest)?\s*=\s*true)(?![\w.-])", normalized):
        return True
    buildx = re.search(r"(?<![\w.-])buildx(?![\w.-])", normalized)
    if buildx is None:
        return False
    return any(
        match.group(1) != "false"
        for match in re.finditer(r"(?<![\w.-])--push(?:=([^\s;|&()]+))?(?![\w.-])", normalized[buildx.end() :])
    )


def script_is_forbidden(script: str, depth: int = 0) -> bool:
    # Ban continuations in every quote context, even safe commands. Ordinary YAML
    # multiline scripts or shell arrays remain available without shell escapes.
    if re.search(r"\\\r?\n", script) or normalized_publisher_text_is_forbidden(script):
        return True
    # Substitutions generate command text at runtime, beyond what this static policy can prove safe.
    if DYNAMIC_SHELL_SYNTAX.search(script):
        return True
    return any(command_is_forbidden(segment, depth) for segment in shell_segments(script))


def command_value_options(command: str) -> set[str]:
    """Recognize data-valued options before executable/subcommand words."""
    if command == "uv":
        return {
            "--directory",
            "--project",
            "--config-file",
            "--cache-dir",
            "--color",
            "--python",
            "-p",
            "--default-index",
            "--index-url",
            "--extra-index-url",
            "--index",
            "--keyring-provider",
            "--allow-insecure-host",
            "--trusted-host",
        }
    if command == "git":
        return {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--config-env"}
    if command == "gh":
        return {"--repo", "-R", "--hostname"}
    if command in {"kubectl", "helm"}:
        return {"--namespace", "-n", "--context", "--kube-context", "--kubeconfig", "--server", "--token"}
    if command == "docker":
        return {"--config", "--context", "-c", "--host", "-H", "--builder"}
    if command in {"cargo", "npm", "pnpm", "yarn", "poetry", "hatch"}:
        return {"--manifest-path", "--config", "--cwd", "--directory", "--prefix", "--registry", "-C"}
    if command == "twine":
        return {"--repository", "--repository-url", "--config-file", "-r"}
    if command == "env":
        return {"-u", "--unset", "-C", "--chdir", "-S", "--split-string", "--argv0", "-a"}
    if command == "sudo":
        return {"-u", "--user", "-g", "--group", "-h", "--host", "-C", "--close-from", "-p", "--prompt"}
    if command == "timeout":
        return {"-s", "--signal", "-k", "--kill-after"}
    if command == "xargs":
        return {"-I", "--replace", "-a", "--arg-file", "-n", "--max-args", "-P", "--max-procs", "-d", "--delimiter"}
    if command == "nice":
        return {"-n", "--adjustment"}
    if command == "stdbuf":
        return {"-i", "--input", "-o", "--output", "-e", "--error"}
    return set()


def next_command_word(tokens: list[str], start: int, command: str, assignments: bool = False) -> int | None:
    """Skip literal options and their data, not expansions that could select a command."""
    value_options = command_value_options(command)
    index = start
    while index < len(tokens):
        token = tokens[index]
        if command == "cargo" and token.startswith("+"):
            index += 1  # rustup toolchain selector, not cargo's subcommand.
        elif assignments and SHELL_ASSIGNMENT.match(token):
            index += 1
        elif token == "--":
            return index + 1 if index + 1 < len(tokens) else None
        elif token in value_options:
            index += 2
        elif token.startswith("-") and not token.startswith("--") and token[:2] in value_options:
            index += 1  # Attached short-option data, including variable paths.
        elif token.startswith("-") and not command_word_is_dynamic(token.split("=", 1)[0]):
            # Unknown option arity could conceal a verb after its operand. Fail closed
            # when expansions remain; known boolean/data options preserve ordinary CI.
            if (
                token not in COMMAND_BOOLEAN_OPTIONS
                and "=" not in token
                and token[:2] not in value_options
                and any(command_word_is_dynamic(argument) for argument in tokens[index + 1 :])
            ):
                return index
            index += 1
        else:
            return index
    return None


def dynamic_publisher_subcommand(tokens: list[str]) -> bool:
    # Literal-only denylist matching missed `uv "$verb"`. Resolve just the known command
    # paths, never shell variable values. Once a safe literal verb is selected, arguments
    # such as `uv run pytest "$TESTS"` remain data rather than possible publisher verbs.
    prefixes = {prefix for prefix in FORBIDDEN_COMMAND_PREFIXES if prefix[0] not in {"python", "python3"}}
    prefixes |= {
        ("docker", "buildx", "build"),
        ("docker", "buildx", "b"),
        ("docker", "build"),
        ("docker", "builder", "build"),
        ("buildx", "build"),
        ("buildx", "b"),
        ("gh", "release", "create"),
        ("kubectl", "apply"),
    }
    for index, token in enumerate(tokens):
        command = token.rsplit("/", 1)[-1].lower()
        for prefix in prefixes:
            if command != prefix[0]:
                continue
            position = index + 1
            for word in prefix[1:]:
                next_index = next_command_word(tokens, position, command)
                if next_index is None:
                    break
                candidate = tokens[next_index]
                if candidate.startswith("-") or command_word_is_dynamic(candidate):
                    return True
                if candidate.lower() != word:
                    break
                position = next_index + 1
        if re.fullmatch(r"python(?:\d+(?:\.\d+)*)?", command):
            arguments = tokens[index + 1 :]
            if "-m" in arguments:
                module_index = arguments.index("-m") + 1
                if module_index < len(arguments) and command_word_is_dynamic(arguments[module_index]):
                    return True
    return False


def buildx_exporter_is_forbidden(value: str) -> bool:
    # --push is only shorthand: registry exporters and image push attributes also
    # publish. Exporter values are CSV, including quoted fields with multiple names.
    # Any expansion can inject additional CSV attributes, even in a dest/name value.
    if command_word_is_dynamic(value):
        return True
    try:
        fields = next(csv.reader([value], strict=True))
    except csv.Error:
        return True
    for field in fields:
        key, separator, setting = field.partition("=")
        if not separator:
            continue  # A bare destination is buildx's local-exporter shorthand.
        key = key.strip().lower()  # buildx trims and lowercases CSV keys.
        if key == "type" and setting.lower() == "registry":
            return True
        if key in {"push", "push-by-digest"} and setting.lower() not in {"false", "0", "f"}:
            return True
    return False


def buildx_outputs_are_forbidden(tokens: list[str]) -> bool:
    for index, token in enumerate(tokens):
        if token in {"--output", "-o"}:
            if index + 1 >= len(tokens) or buildx_exporter_is_forbidden(tokens[index + 1]):
                return True
        elif token.startswith("--output="):
            if command_word_is_dynamic(token):
                return True
            raw = token.raw if isinstance(token, ShellWord) else token
            value = ShellWord(token.split("=", 1)[1], raw.split("=", 1)[1])
            if buildx_exporter_is_forbidden(value):
                return True
        elif token.startswith("-o") and token != "-o":
            if command_word_is_dynamic(token):
                return True
            raw = token.raw if isinstance(token, ShellWord) else token
            value = ShellWord(token[2:].removeprefix("="), raw[2:].removeprefix("="))
            if buildx_exporter_is_forbidden(value):
                return True
    return False


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
        # Control-flow introducers are shell syntax, not executable words. The
        # following word still selects an executable and needs expansion checks.
        while command in SHELL_COMMAND_PREFIXES and command_index + 1 < len(tokens):
            command_index += 1
            command = normalized[command_index]
        # eval/source and variable command words can execute candidate-generated text that this
        # validator never sees. Reject the indirection rather than trying to emulate a shell.
        if command in {".", "eval", "source"} or (
            command not in {"[", "[["} and command_word_is_dynamic(tokens[command_index])
        ):
            return True
        while command in DYNAMIC_COMMAND_WRAPPERS:
            # The former all-arguments check rejected `env pytest "$TESTS"`. Only the
            # wrapped executable can introduce command text; later operands are data.
            wrapped_index = next_command_word(tokens, command_index + 1, command, assignments=True)
            if command == "timeout" and wrapped_index is not None:
                if command_word_is_dynamic(tokens[wrapped_index]):
                    return True
                wrapped_index += 1  # timeout's duration precedes its executable.
            if wrapped_index is None or wrapped_index >= len(tokens):
                break
            command_index = wrapped_index
            command = normalized[command_index]
            if (
                tokens[command_index].startswith("-")
                or command_word_is_dynamic(tokens[command_index])
                or command in {".", "eval", "source"}
            ):
                return True

    if dynamic_publisher_subcommand(tokens):
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
    if any(
        contains_ordered(prefix)
        for prefix in (
            ("docker", "buildx", "build"),
            ("docker", "buildx", "b"),
            ("docker", "build"),
            ("docker", "builder", "build"),
            ("buildx", "build"),
            ("buildx", "b"),
        )
    ) and (push_enabled or buildx_outputs_are_forbidden(tokens)):
        return True

    if any(contains_ordered(("gh", "release", command)) for command in FORBIDDEN_GH_RELEASE_COMMANDS):
        return True
    if any(contains_ordered(("kubectl", command)) for command in FORBIDDEN_KUBECTL_COMMANDS):
        return True

    payloads = nested_command_payloads(tokens)
    if payloads and depth >= MAX_NESTED_PAYLOAD_DEPTH:
        return True
    return any(script_is_forbidden(payload, depth + 1) for payload in payloads)


def command_text_policy_errors(scope: str, value: Any) -> list[str]:
    if isinstance(value, str):
        if re.search(r"\\\r?\n", value):
            return [f"{scope}: publishing, release, or deployment step is forbidden (shell line continuation)"]
        try:
            forbidden = script_is_forbidden(value)
        except ValueError:
            forbidden = True
        if forbidden:
            return [f"{scope}: publishing, release, or deployment step is forbidden"]
    return []


def default_shell_policy_errors(scope: str, defaults: Any) -> list[str]:
    if not isinstance(defaults, dict):
        return []
    run = defaults.get("run")
    if not isinstance(run, dict):
        return []
    return command_text_policy_errors(f"{scope} defaults.run.shell", run.get("shell"))


def step_policy_errors(scope: str, step: Any) -> list[str]:
    if not isinstance(step, dict):
        return [f"{scope}: step must be a mapping"]

    uses = step.get("uses")
    if "uses" in step:
        # A positive executable-action allowlist also rejects every publisher,
        # local action, and reusable workflow, regardless of its ref or inputs.
        if not isinstance(uses, str) or uses.lower() not in ALLOWED_STEP_ACTIONS:
            return [f"{scope}: publishing, release, or deployment step {uses!r} is forbidden"]

    # The runner executes the shell template too; inspecting only run misses publishing there.
    return command_text_policy_errors(f"{scope} run", step.get("run")) + command_text_policy_errors(
        f"{scope} shell", step.get("shell")
    )


MERGIFY_ALTERNATE_CONFIGS = (
    ".mergify.yaml",
    ".mergify/config.yml",
    ".mergify/config.yaml",
    ".github/mergify.yml",
    ".github/mergify.yaml",
)


def mergify_policy_errors(root: Path) -> list[str]:
    """Pin the trusted merge-queue audience and checks in candidate config."""
    # Mergify reads the first of several config locations. Only .mergify.yml is
    # validated, so every other location is forbidden rather than left unchecked.
    errors: list[str] = [
        f"{name}: Mergify configuration must live in .mergify.yml"
        for name in MERGIFY_ALTERNATE_CONFIGS
        if (root / name).exists() or (root / name).is_symlink()
    ]
    path = root / ".mergify.yml"
    if not path.exists() and not path.is_symlink():
        return errors
    if path.is_symlink():
        return errors + [".mergify.yml: symbolic link is forbidden"]
    try:
        config = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        return errors + [f".mergify.yml: invalid YAML: {exc}"]
    if not isinstance(config, dict):
        return errors + [".mergify.yml: configuration must be a mapping"]

    required = set(REQUIRED_MERGIFY_CONDITIONS)

    if "extends" in config:
        errors.append(".mergify.yml: extends is forbidden; the pinned policy must be self-contained")
    rules = config.get("pull_request_rules") or []
    if not isinstance(rules, list):
        errors.append(".mergify.yml: pull_request_rules must be a list")
    else:
        for index, rule in enumerate(rules):
            actions = rule.get("actions") if isinstance(rule, dict) else None
            if not isinstance(actions, dict) or {"queue", "merge"} & set(actions):
                errors.append(
                    f".mergify.yml: pull_request_rules[{index}] must not queue or merge; use queue_rules"
                )

    def check_conditions(scope: str, value: Any) -> None:
        if not isinstance(value, list) or not all(isinstance(condition, str) for condition in value):
            errors.append(f".mergify.yml: {scope} must be a list of condition strings")
            return
        missing = sorted(required - set(value))
        if missing:
            errors.append(f".mergify.yml: {scope} is missing required conditions: {', '.join(missing)}")

    queue_rules = config.get("queue_rules")
    if not isinstance(queue_rules, list) or not queue_rules:
        errors.append(".mergify.yml: queue_rules must be a non-empty list")
    else:
        for index, rule in enumerate(queue_rules):
            if not isinstance(rule, dict):
                errors.append(f".mergify.yml: queue_rules[{index}] must be a mapping")
                continue
            check_conditions(f"queue_rules[{index}].queue_conditions", rule.get("queue_conditions"))
            check_conditions(f"queue_rules[{index}].merge_conditions", rule.get("merge_conditions"))

    protections = config.get("merge_protections_settings")
    if not isinstance(protections, dict):
        errors.append(".mergify.yml: merge_protections_settings must be a mapping")
    else:
        check_conditions("merge_protections_settings.auto_merge_conditions", protections.get("auto_merge_conditions"))
    return errors


def validate(root: Path) -> list[str]:
    errors: list[str] = []
    if root.is_symlink():
        return ["candidate root: symbolic link is forbidden"]
    try:
        resolved_root = root.resolve(strict=True)
    except OSError as exc:
        return [f"candidate root: cannot resolve policy input: {exc}"]

    errors.extend(mergify_policy_errors(root))
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
    expected_events = dict(EXPECTED_EVENTS)
    if "fork-perf.yml" in actual and "perf-test.yml" not in actual:
        expected_events["fork-perf.yml"] = expected_events.pop("perf-test.yml")
        try:
            digest = hashlib.sha256((workflow_dir / "fork-perf.yml").read_bytes()).hexdigest()
        except OSError as exc:
            errors.append(f"fork-perf.yml: cannot read renamed workflow: {exc}")
        else:
            if digest != RENAMED_PERF_SHA256:
                errors.append("fork-perf.yml: renamed workflow must match the reviewed perf-test.yml bytes")
    expected = set(expected_events)

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

        if events != expected_events[name]:
            errors.append(
                f"{name}: events {sorted(events)} do not match allowed events {sorted(expected_events[name])}"
            )
        if name == "fork-ci.yml" and workflow.get("on") != EXPECTED_FORK_CI_TRIGGER:
            errors.append(f"{name}: trigger configuration must exactly target main and allow manual dispatch")
        if name == "gate.yml" and workflow.get("on") not in (EXPECTED_GATE_TRIGGER, QUEUE_GATE_TRIGGER):
            errors.append(f"{name}: trigger configuration must exactly target pull requests into main")
        if name == "nightly.yml" and workflow.get("on") != EXPECTED_NIGHTLY_TRIGGER:
            errors.append(f"{name}: trigger configuration must be exactly the reviewed schedule and manual dispatch")
        if name == "fork-policy.yml" and workflow != EXPECTED_FORK_POLICY_WORKFLOW:
            errors.append(f"{name}: trusted policy workflow must exactly match the reviewed configuration")
        errors.extend(sensitive_capability_errors(name, workflow))
        # Check every authored default, even when a job or step overrides it.
        errors.extend(default_shell_policy_errors(name, workflow.get("defaults")))
        # Authoritative capability boundary: no write scope (including OIDC or
        # packages), no publishing credentials, and only reviewed safe actions.
        errors.extend(permission_errors(f"{name}: top-level permissions", workflow.get("permissions")))

        jobs = workflow.get("jobs")
        if not isinstance(jobs, dict) or not jobs:
            errors.append(f"{name}: jobs must be a non-empty mapping")
        else:
            for job_name, job in jobs.items():
                if not isinstance(job, dict):
                    errors.append(f"{name} job {job_name!r}: job must be a mapping")
                    continue
                if "permissions" in job:
                    errors.extend(permission_errors(f"{name} job {job_name!r}", job["permissions"]))
                if "uses" in job:
                    errors.append(f"{name} job {job_name!r}: reusable workflow call is forbidden")
                    continue
                errors.extend(default_shell_policy_errors(f"{name} job {job_name!r}", job.get("defaults")))
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
