#!/usr/bin/env python3
"""Contributor CI: explicit setup, then a credential-free maintained fork gate."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from database import DisposablePostgres
from models import prepare, verify

ROOT = Path(__file__).resolve().parents[2]
STATE = ROOT / ".ci"
PYTHON = ROOT / ".venv/bin/python"
API = ROOT / "hindsight-api-slim"
UV_VERSION = "0.9.28"
REGRESSIONS = (
    "codex_quota_reset_defer",
    "provider_quota_reset_defer",
    "op_cancellation",
    "worker",
    "ocr_quality",
    "consolidation_failure_isolation",
    "consolidation_prompt_budget",
    "db_abstraction",
    "language_integrity",
    "language_integrity_retain",
    "consolidation_retry_budget",
    "fact_extraction_retry",
    "language_prevention",
    "language_prevention_atomicity",
    "language_prevention_dedup",
    "language_prevention_review",
    "language_code_spans",
    "consolidation_language_sources",
    "consolidation_batch_atomicity",
    "consolidation_dedup",
    "worker_retry_knobs",
    "content_policy_refusal_permanent",
    "operation_status",
    "response_schema_validation",
    "metrics",
    "batch_api",
)


def environment(home: Path) -> dict[str, str]:
    home.mkdir(parents=True, exist_ok=True)
    scratch = home / "tmp"
    scratch.mkdir(exist_ok=True)
    return {
        "PATH": str(ROOT / ".venv/bin") + os.pathsep + os.environ.get("PATH", os.defpath),
        "HOME": str(home),
        "TMPDIR": str(scratch),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "XDG_CONFIG_HOME": str(home / ".config"),
        "XDG_CACHE_HOME": str(home / ".cache"),
        "XDG_DATA_HOME": str(home / ".local/share"),
        "XDG_STATE_HOME": str(home / ".local/state"),
        "UV_CACHE_DIR": str(STATE / "cache/uv"),
        "UV_PYTHON_INSTALL_DIR": str(STATE / "python"),
        "UV_PROJECT_ENVIRONMENT": str(ROOT / ".venv"),
        "UV_NO_CONFIG": "1",
        "GOCACHE": str(STATE / "cache/go-build"),
        "GOMODCACHE": str(STATE / "cache/go-mod"),
        "GOTOOLCHAIN": "local",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_TERMINAL_PROMPT": "0",
        "PYTHONNOUSERSITE": "1",
        "PYTHONPATH": str(ROOT),
        "HF_HOME": str(STATE / "cache/huggingface"),
        "HF_HUB_DISABLE_TELEMETRY": "1",
        "HF_HUB_DISABLE_IMPLICIT_TOKEN": "1",
        "TOKENIZERS_PARALLELISM": "false",
        "OMP_NUM_THREADS": "2",
        "MKL_NUM_THREADS": "2",
        "CI": "true",
    }


def stop_process_group(process: subprocess.Popen) -> None:
    # The leader may already have exited while a child still owns the session.
    # Reap the leader and check the complete group after each shutdown signal.
    for shutdown_signal, grace in ((signal.SIGINT, 20), (signal.SIGKILL, 5)):
        process.poll()
        try:
            os.killpg(process.pid, shutdown_signal)
        except ProcessLookupError:
            return
        deadline = time.monotonic() + grace
        while time.monotonic() < deadline:
            process.poll()
            try:
                os.killpg(process.pid, 0)
            except ProcessLookupError:
                return
            time.sleep(0.05)
    raise RuntimeError(f"Command process group {process.pid} remains after cleanup")


def run(args: list[str], env: dict[str, str], cwd: Path = ROOT, timeout: int = 1800) -> None:
    print("+ " + " ".join(map(str, args)), flush=True)
    process = subprocess.Popen(args, cwd=cwd, env=env, start_new_session=True)
    try:
        code = process.wait(timeout=timeout)
        if code:
            raise subprocess.CalledProcessError(code, args)
    finally:
        original_error = sys.exc_info()[1]
        try:
            stop_process_group(process)
        except Exception as cleanup_error:
            if original_error is None:
                raise
            # Cleanup diagnostics must not conceal the original failed command.
            print(f"Additional cleanup failure: {cleanup_error}", file=sys.stderr)


def lock_hash() -> str:
    return hashlib.sha256((ROOT / "uv.lock").read_bytes()).hexdigest()


def setup() -> None:
    env = environment(STATE / "setup-home")
    run(
        [
            "uv",
            "sync",
            "--locked",
            "--python",
            (ROOT / ".python-version").read_text().strip(),
            "--package",
            "hindsight-api-slim",
            "--package",
            "hindsight-client",
            "--extra",
            "all",
            "--group",
            "dev",
        ],
        env,
    )
    # The launcher may start with a system Python older than the locked 3.11.
    run([str(PYTHON), str(Path(__file__)), "prepare-models"], env)
    run(["go", "mod", "download"], env, ROOT / "hindsight-clients/go")
    run(["uv", "build", "--package", "hindsight-api-slim", "--out-dir", str(STATE / "preparation-dist")], env)
    (STATE / "setup.json").write_text(json.dumps({"lock": lock_hash()}) + "\n")


def check() -> None:
    if not PYTHON.exists() or not (STATE / "setup.json").is_file():
        raise RuntimeError("Run ./bin/ci setup first")
    if json.loads((STATE / "setup.json").read_text())["lock"] != lock_hash():
        raise RuntimeError("uv.lock changed; run ./bin/ci setup")
    verify(STATE / "models")
    runs = STATE / "runs"
    runs.mkdir(exist_ok=True)
    scratch = Path(tempfile.mkdtemp(prefix="run-", dir=runs))
    env = environment(scratch / "home")
    env.update(HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1", HINDSIGHT_CI_MODELS=str(STATE / "models"))
    env.update(GOPROXY="off", GOSUMDB="off")
    success = False
    try:
        run([str(PYTHON), "-m", "unittest", "discover", "-s", "tests/ci", "-v"], env)
        run([str(PYTHON), "scripts/ci/validate_fork_workflows.py"], env)
        run([str(ROOT / ".venv/bin/ruff"), "check", "scripts/ci"], env)
        run([str(ROOT / ".venv/bin/ruff"), "format", "--check", "scripts/ci", "tests/ci"], env)
        for args in (["ruff", "check", "."], ["ruff", "format", "--check", "."], ["ty", "check", "hindsight_api"]):
            run([str(ROOT / ".venv/bin" / args[0]), *args[1:]], env, API)
        with DisposablePostgres(scratch / "postgres", env) as database:
            env["HINDSIGHT_CI_DATABASE_URL"] = database.uri
            run(
                [
                    str(PYTHON),
                    "-m",
                    "pytest",
                    "-p",
                    "scripts.ci.pytest_portable",
                    "--portable-ci",
                    "-n",
                    "0",
                    *[f"tests/test_{name}.py" for name in REGRESSIONS],
                ],
                env,
                API,
                timeout=3600,
            )
        env.pop("HINDSIGHT_CI_DATABASE_URL")
        run(
            [str(PYTHON), "-m", "pytest", "-n", "0", "tests/test_operation_details.py"],
            env,
            ROOT / "hindsight-clients/python",
        )
        run(
            ["go", "test", "-mod=readonly", "-run", "^TestOperationResponseDetailsUseDiscriminator$", "."],
            env,
            ROOT / "hindsight-clients/go",
        )
        run(["uv", "build", "--package", "hindsight-api-slim", "--offline", "--out-dir", str(scratch / "dist")], env)
        run([str(PYTHON), "-m", "compileall", "-q", "hindsight_api"], env, API)
        run([str(PYTHON), "-c", "import hindsight_api, hindsight_api.config, hindsight_api.main"], env, API)
        run(["git", "diff", "--check"], env)
        success = True
    finally:
        if success:
            shutil.rmtree(scratch)
        else:
            print(f"Failed run diagnostics retained: {scratch}", file=sys.stderr)


def interrupt(_signal: int, _frame: object) -> None:
    raise KeyboardInterrupt


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command", nargs="?", default="all", choices=("all", "setup", "check", "list", "prepare-models")
    )
    args = parser.parse_args()
    if args.command == "list":
        print(
            "policy + harness tests; API Ruff/format/types; 26 API regressions with real local PG/models; "
            "Python/Go client discriminator; API build/compile/import; whitespace"
        )
        return
    signal.signal(signal.SIGTERM, interrupt)
    if args.command in ("all", "setup", "check"):
        version = subprocess.check_output(["uv", "--version"], text=True, timeout=10).split()
        if version[:2] != ["uv", UV_VERSION]:
            raise RuntimeError(
                f"CI requires uv {UV_VERSION} on PATH for its prepared build cache, got {' '.join(version)}"
            )
    if args.command == "prepare-models":
        prepare(STATE / "models")
        return
    if args.command in ("setup", "all"):
        setup()
    if args.command in ("check", "all"):
        # Use exactly the selected worktree's environment, including first run.
        if Path(sys.executable).absolute() != PYTHON.absolute():
            os.execve(str(PYTHON), [str(PYTHON), str(Path(__file__)), "check"], environment(STATE / "setup-home"))
        check()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        raise SystemExit(130)
    except (RuntimeError, OSError, subprocess.SubprocessError, ValueError, KeyError) as error:
        print(f"CI failed: {error}", file=sys.stderr)
        raise SystemExit(1)
