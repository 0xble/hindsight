"""Check invocation isolation and failure behavior at the harness boundary."""

from contextlib import ExitStack, redirect_stderr
import io
import json
import os
from pathlib import Path
import signal
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts/ci"))
import run as ci
from database import DisposablePostgres, wait_for_exit
from models import MODELS, file_hash, verify


class HarnessTests(unittest.TestCase):
    def test_wrong_uv_fails_before_the_gate_starts(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            tools = root / "tools"
            tools.mkdir()
            uv = tools / "uv"
            uv.write_text("#!/bin/sh\necho 'uv 99.0.0'\n")
            uv.chmod(0o755)
            scripts = root / "scripts/ci"
            scripts.mkdir(parents=True)
            for name in ("run.py", "database.py", "models.py"):
                shutil.copyfile(ci.ROOT / "scripts/ci" / name, scripts / name)
            env = ci.environment(root / "home")
            env["PATH"] = str(tools) + os.pathsep + env["PATH"]
            result = subprocess.run(
                [sys.executable, str(scripts / "run.py"), "check"], env=env, capture_output=True, text=True, timeout=15
            )
            self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
            self.assertIn("requires uv 0.9.28", result.stderr)
            self.assertNotIn("unittest", result.stdout)

    def test_personal_environment_is_not_inherited(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(
                os.environ,
                {
                    "OPENAI_API_KEY": "private",
                    "HINDSIGHT_API_DATABASE_URL": "private",
                    "HF_TOKEN": "private",
                    "PYTHONPATH": "/another/checkout",
                    "UV_PROJECT_ENVIRONMENT": "/another/venv",
                    "GIT_CONFIG_COUNT": "1",
                },
            ),
        ):
            env = ci.environment(Path(directory))
            for key in ("OPENAI_API_KEY", "HINDSIGHT_API_DATABASE_URL", "HF_TOKEN", "GIT_CONFIG_COUNT"):
                self.assertNotIn(key, env)
            self.assertEqual(env["UV_PROJECT_ENVIRONMENT"], str(ci.ROOT / ".venv"))
            self.assertEqual(env["PYTHONPATH"], str(ci.ROOT))
            self.assertEqual(env["HOME"], directory)
            self.assertEqual(env["TMPDIR"], str(Path(directory) / "tmp"))

    def test_subprocess_failure_is_not_green(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(subprocess.CalledProcessError) as result:
                ci.run([sys.executable, "-c", "raise SystemExit(19)"], ci.environment(Path(directory)))
            self.assertEqual(result.exception.returncode, 19)

    def test_exited_leader_does_not_leave_descendants(self):
        for code in (0, 19):
            with self.subTest(exit_code=code), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                child = root / "child.py"
                child.write_text(
                    "import os, time\nfrom pathlib import Path\n"
                    f"Path({str(root / 'ready')!r}).write_text(str(os.getpid()))\n"
                    "time.sleep(60)\n"
                )
                leader = root / "leader.py"
                leader.write_text(
                    "import subprocess, sys, time\nfrom pathlib import Path\n"
                    f"subprocess.Popen([sys.executable, {str(child)!r}])\n"
                    f"while not Path({str(root / 'ready')!r}).exists(): time.sleep(0.01)\n"
                    f"raise SystemExit({code})\n"
                )
                env = ci.environment(root / "home")
                try:
                    if code:
                        with self.assertRaises(subprocess.CalledProcessError) as result:
                            ci.run([sys.executable, str(leader)], env)
                        self.assertEqual(result.exception.returncode, code)
                    else:
                        ci.run([sys.executable, str(leader)], env)
                    with self.assertRaises(ProcessLookupError):
                        os.kill(int((root / "ready").read_text()), 0)
                finally:
                    if (root / "ready").exists():
                        try:
                            os.kill(int((root / "ready").read_text()), signal.SIGKILL)
                        except ProcessLookupError:
                            pass

    def test_timeout_interrupts_and_reaps_child(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            script = root / "child.py"
            script.write_text(
                "import os, time\nfrom pathlib import Path\n"
                f"Path({str(root / 'pid')!r}).write_text(str(os.getpid()))\n"
                "time.sleep(60)\n"
            )
            with self.assertRaises(subprocess.TimeoutExpired):
                ci.run([sys.executable, str(script)], ci.environment(root / "home"), timeout=1)
            with self.assertRaises(ProcessLookupError):
                os.kill(int((root / "pid").read_text()), 0)

    def test_missing_and_changed_model_inputs_fail(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaises(FileNotFoundError):
                verify(root)
            for model in MODELS:
                destination = root / model.name / model.revision
                files = {}
                for name in (
                    "config.json",
                    "model.safetensors",
                    "tokenizer_config.json",
                    "modules.json",
                    "1_Pooling/config.json",
                ):
                    path = destination / name
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text("fixture")
                    files[name] = file_hash(path)
                (root / f"{model.name}.json").write_text(
                    json.dumps(
                        {
                            "repository": model.repository,
                            "revision": model.revision,
                            "files": files,
                        }
                    )
                )
            verify(root)
            (root / MODELS[0].name / MODELS[0].revision / "model.safetensors").write_text("changed")
            with self.assertRaisesRegex(RuntimeError, "Modified embedding"):
                verify(root)

    def test_skipped_maintained_coverage_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "test_skip.py").write_text('import pytest\ndef test_required(): pytest.skip("missing input")\n')
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "pytest",
                    "-o",
                    "addopts=",
                    "-p",
                    "scripts.ci.pytest_portable",
                    "--portable-ci",
                    str(root / "test_skip.py"),
                ],
                cwd=root,
                env=ci.environment(root / "home"),
                capture_output=True,
                text=True,
                timeout=60,
            )
            self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
            self.assertIn("Maintained CI coverage was skipped", result.stdout)

    def test_optional_hooks_stay_in_selected_worktree(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first, second = root / "first", root / "second"
            first.mkdir()
            env = ci.environment(root / "home")

            def git(*arguments, cwd=first):
                return subprocess.run(["git", *arguments], cwd=cwd, env=env, capture_output=True, text=True, check=True)

            git("init")
            git(
                "-c",
                "user.name=CI",
                "-c",
                "user.email=ci@example.invalid",
                "-c",
                "commit.gpgsign=false",
                "commit",
                "--allow-empty",
                "-m",
                "fixture",
            )
            git("worktree", "add", "-b", "second", str(second))
            subprocess.run(
                [str(ci.ROOT / "scripts/setup-hooks.sh")],
                cwd=second,
                env=env,
                capture_output=True,
                text=True,
                check=True,
            )
            self.assertEqual(git("config", "--get", "core.hooksPath", cwd=second).stdout.strip(), ".githooks")
            result = subprocess.run(
                ["git", "config", "--get", "core.hooksPath"], cwd=first, env=env, capture_output=True, text=True
            )
            self.assertEqual(result.returncode, 1, result.stdout + result.stderr)


class DatabaseTests(unittest.TestCase):
    def test_cleanup_error_does_not_replace_startup_error(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = DisposablePostgres(root / "postgres", ci.environment(root / "home"))
            output = io.StringIO()
            with (
                patch.object(database, "command", side_effect=RuntimeError("original startup failed")),
                patch.object(database, "close", side_effect=RuntimeError("cleanup failed")),
                redirect_stderr(output),
                self.assertRaisesRegex(RuntimeError, "original startup failed"),
            ):
                database.__enter__()
            self.assertIn("Additional PostgreSQL cleanup failure: cleanup failed", output.getvalue())

    def test_missing_process_tools_fail_before_database_allocation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            env = ci.environment(root / "home")
            env["PATH"] = str(root / "empty-tools")
            with self.assertRaisesRegex(RuntimeError, "kill.*ps.*procps"):
                DisposablePostgres(root / "postgres", env)
            self.assertFalse((root / "postgres").exists())

    def test_successful_stop_that_leaves_pidfile_uses_owned_pg_ctl(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = DisposablePostgres(root / "postgres", ci.environment(root / "home"))
            try:
                database.__enter__()
                command = database.command

                def false_stop(*args, **kwargs):
                    if args[0] == "stop":
                        return subprocess.CompletedProcess(args, 0, "not running", "")
                    return command(*args, **kwargs)

                with patch.object(database, "command", side_effect=false_stop):
                    database.close()
                self.assertFalse((database.data / "postmaster.pid").exists())
            finally:
                database.close()

    def test_exit_observation_accepts_zombie_but_not_live_process(self):
        child = subprocess.Popen([sys.executable, "-c", "raise SystemExit(0)"])
        try:
            # Deliberately do not poll/wait: retain an exited child for observation.
            wait_for_exit(child.pid)
        finally:
            child.wait(timeout=5)
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        try:
            with self.assertRaisesRegex(RuntimeError, "remains live"):
                wait_for_exit(child.pid, timeout=0.1)
        finally:
            child.terminate()
            child.wait(timeout=5)

    def test_concurrent_clusters_are_distinct_and_both_clean_up(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            databases = []
            with ExitStack() as stack:
                for name in ("first", "second"):
                    databases.append(
                        stack.enter_context(DisposablePostgres(root / name, ci.environment(root / "home")))
                    )
                self.assertNotEqual(databases[0].uri, databases[1].uri)
                self.assertNotEqual(databases[0].data, databases[1].data)
                first = databases[0].command(
                    "psql", "--name", databases[0].name, "-XAt", "-c", "CREATE TABLE invocation_marker (id integer)"
                )
                self.assertEqual(first.returncode, 0, first.stderr)
                other = databases[1].command(
                    "psql",
                    "--name",
                    databases[1].name,
                    "-XAt",
                    "-c",
                    "SELECT count(*) FROM pg_tables WHERE tablename='invocation_marker'",
                )
                self.assertEqual(other.returncode, 0, other.stderr)
                self.assertEqual(other.stdout.strip(), "0")
            for database in databases:
                self.assertFalse((database.data / "postmaster.pid").exists())

    def test_exception_stops_owned_cluster(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = DisposablePostgres(root / "postgres", ci.environment(root / "home"))
            with self.assertRaises(KeyboardInterrupt):
                with database:
                    raise KeyboardInterrupt
            self.assertFalse((database.data / "postmaster.pid").exists())

    def test_sigterm_stops_subprocess_and_database(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            script = root / "owner.py"
            child = root / "child.py"
            child.write_text(
                "import os, time\nfrom pathlib import Path\n"
                f"Path({str(root / 'ready')!r}).write_text(str(os.getpid()))\n"
                "time.sleep(60)\n"
            )
            script.write_text(
                "import signal, sys\nfrom pathlib import Path\n"
                f"sys.path.insert(0, {str(ci.ROOT / 'scripts/ci')!r})\n"
                "import run as ci\nfrom database import DisposablePostgres\n"
                "signal.signal(signal.SIGTERM, ci.interrupt)\n"
                f"root = Path({str(root)!r})\n"
                "try:\n"
                "    with DisposablePostgres(root / 'postgres', ci.environment(root / 'home')):\n"
                "        ci.run([sys.executable, str(root / 'child.py')], ci.environment(root / 'home'))\n"
                "except KeyboardInterrupt:\n"
                "    raise SystemExit(130)\n"
            )
            process = subprocess.Popen(
                [sys.executable, str(script)],
                cwd=ci.ROOT,
                env=ci.environment(root / "home"),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
            try:
                deadline = time.monotonic() + 30
                while not (root / "ready").exists() and time.monotonic() < deadline and process.poll() is None:
                    time.sleep(0.05)
                self.assertTrue((root / "ready").exists(), "Owned child did not start")
                process.send_signal(signal.SIGTERM)
                output, _ = process.communicate(timeout=30)
                self.assertEqual(process.returncode, 130, output)
                self.assertFalse((root / "postgres/data/postmaster.pid").exists())
                with self.assertRaises(ProcessLookupError):
                    os.kill(int((root / "ready").read_text()), 0)
            finally:
                if process.poll() is None:
                    process.send_signal(signal.SIGTERM)
                    process.communicate(timeout=30)


if __name__ == "__main__":
    unittest.main()
