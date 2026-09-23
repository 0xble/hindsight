"""One invocation-owned pg0 cluster, including checked shutdown on failure."""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit


def wait_for_exit(pid: int, timeout: float = 5) -> None:
    """Observe exit without signaling a PID that could already have been reused."""
    deadline = time.monotonic() + timeout
    while True:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        if sys.platform.startswith("linux"):
            try:
                # /proc stat field 3 follows the parenthesized process name.
                state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
            except FileNotFoundError:
                return
        else:
            result = subprocess.run(["ps", "-p", str(pid), "-o", "stat="], capture_output=True, text=True, timeout=5)
            if result.returncode == 1 and not result.stdout.strip():
                return
            result.check_returncode()
            state = result.stdout.strip()[:1]
        # A zombie has exited and holds no database files. init may reap it
        # after pg_ctl reports success, so kill(pid, 0) alone is not a liveness test.
        if state in ("Z", "X"):
            return
        if time.monotonic() >= deadline:
            raise RuntimeError(f"PID {pid} remains live after checked PostgreSQL shutdown")
        time.sleep(0.05)


class DisposablePostgres:
    def __init__(self, root: Path, environment: dict[str, str]) -> None:
        missing = [name for name in ("kill", "ps") if not shutil.which(name, path=environment.get("PATH", os.defpath))]
        if missing:
            raise RuntimeError(f"CI requires executable {' and '.join(missing)} on PATH (procps on Debian/Ubuntu)")
        from pg0 import _get_bundled_binary

        binary = _get_bundled_binary()
        if binary is None:
            raise RuntimeError("The locked pg0 platform wheel is required; run ./bin/ci setup")
        self.binary = str(binary)
        self.root = root.resolve()
        self.data = self.root / "data"
        self.name = "hindsight-ci-" + uuid.uuid4().hex
        self.environment = {**environment, "HOME": str(self.root / "home")}
        self.uri = ""
        self.root.mkdir(parents=True, exist_ok=False)
        Path(self.environment["HOME"]).mkdir()

    def command(self, *args: str, timeout: int = 90) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [self.binary, *args], env=self.environment, text=True, capture_output=True, timeout=timeout
        )

    def __enter__(self) -> DisposablePostgres:
        try:
            for attempt in range(3):
                # pg0 0.15 records port=0 incorrectly. Pick a nonzero port and
                # retry only an actual bind collision, never attach to its owner.
                with socket.socket() as listener:
                    listener.bind(("127.0.0.1", 0))
                    port = listener.getsockname()[1]
                result = self.command(
                    "start",
                    "--name",
                    self.name,
                    "--port",
                    str(port),
                    "--data-dir",
                    str(self.data),
                    "--username",
                    "postgres",
                    "--password",
                    uuid.uuid4().hex,
                    "--database",
                    "hindsight",
                    "-c",
                    "listen_addresses=127.0.0.1",
                    "-c",
                    "unix_socket_directories=",
                    "-c",
                    "max_connections=100",
                    timeout=180,
                )
                (self.root / "start.log").write_text(result.stdout + result.stderr)
                if result.returncode == 0:
                    break
                self.close()
                if attempt == 2 or "address already in use" not in (result.stdout + result.stderr).lower():
                    raise RuntimeError(f"Owned pg0 startup failed; see {self.root / 'start.log'}")
            info = self.command("info", "--name", self.name, "-o", "json")
            info.check_returncode()
            value = json.loads(info.stdout)
            if not value["running"] or value["name"] != self.name or Path(value["data_dir"]).resolve() != self.data:
                raise RuntimeError("pg0 returned another instance")
            self.uri = value["uri"]
            address = urlsplit(self.uri)
            if address.hostname != "127.0.0.1" or address.port != port or address.path != "/hindsight":
                raise RuntimeError("pg0 returned an unexpected database URL")
            probe = self.command(
                "psql", "--name", self.name, "-XAt", "-v", "ON_ERROR_STOP=1", "-c", "SHOW data_directory"
            )
            probe.check_returncode()
            if Path(probe.stdout.strip()).resolve() != self.data:
                raise RuntimeError("Connected database is not invocation-owned")
            return self
        except BaseException:
            self.__exit__(*sys.exc_info())
            raise

    def close(self) -> None:
        pidfile = self.data / "postmaster.pid"
        if not pidfile.exists():
            return
        contents = pidfile.read_text().splitlines()
        if len(contents) < 2 or Path(contents[1]).resolve() != self.data:
            raise RuntimeError("Refusing cleanup of a database outside this invocation")
        result = self.command("stop", "--name", self.name, "--timeout", "20", timeout=30)
        # pg0 can report a successful stop without stopping the cluster, or
        # startup can fail before it saves instance.json. The owned pidfile
        # remains authoritative for the existing pg_ctl fallback.
        if pidfile.exists():
            candidates = list((Path(self.environment["HOME"]) / ".pg0/installation").glob("*/bin/pg_ctl"))
            if len(candidates) != 1:
                raise RuntimeError(f"Cannot locate owned pg_ctl; retained {self.root}")
            result = subprocess.run(
                [str(candidates[0]), "stop", "-D", str(self.data), "-m", "fast", "-w", "-t", "20"],
                env=self.environment,
                text=True,
                capture_output=True,
                timeout=30,
            )
        (self.root / "stop.log").write_text(result.stdout + result.stderr)
        if result.returncode or pidfile.exists():
            raise RuntimeError(f"Owned PostgreSQL cleanup failed; retained {self.root}")
        # Verify process exit too, allowing an exited zombie awaiting init reaping.
        wait_for_exit(int(contents[0]))

    def __exit__(self, _kind: object, error: BaseException | None, _traceback: object) -> None:
        try:
            self.close()
        except Exception as cleanup_error:
            if error is None:
                raise
            print(f"Additional PostgreSQL cleanup failure: {cleanup_error}", file=sys.stderr)
