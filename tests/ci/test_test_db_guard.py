"""Offline regression tests: never open a database connection to test the guard."""

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
HELPER = ROOT / "scripts/ci/test_db_guard.py"


@pytest.fixture
def guard():
    spec = importlib.util.spec_from_file_location("test_db_guard", HELPER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    "url",
    [
        "postgresql://u:secret@127.0.0.1:5436/hindsight",
        "postgresql://localhost:5436/other",
        "postgresql://[::1]:5436/hindsight",
        "postgresql://remote.example:5436/disposable",
        "postgresql+asyncpg://u:secret@localhost:5436/db",
        "POSTGRESQL://LOCALHOST:5436/db?sslmode=disable",
        "pg0://instance:5436",
        "PG0://instance:5436?max_connections=300",
        "postgresql://localhost/db?port=5436",
        "postgresql://localhost:5432/db?port=5436",
    ],
)
def test_refuses_production_port(guard, url):
    with pytest.raises(ValueError, match="HINDSIGHT_API_DATABASE_URL") as exc:
        guard.assert_safe_database_url(url)
    assert "secret" not in str(exc.value)
    assert "[REDACTED]" in str(exc.value)
    assert "disposable" in str(exc.value)


@pytest.mark.parametrize(
    "url",
    [
        "postgresql:///db?host=localhost:5436",
        "postgresql:///db?host=localhost:5575&host=remote:5436",
        "postgresql:///db?host=localhost:5575,remote:5436",
        "postgresql:///db?host=%5B%3A%3A1%5D%3A5436",
        "postgresql:///db?host=/tmp/socket:5436",
    ],
)
def test_refuses_query_host_ports(guard, url):
    with pytest.raises(ValueError, match="5436"):
        guard.assert_safe_database_url(url)


@pytest.mark.parametrize(
    "url",
    [
        None,
        "postgresql://localhost/db",
        "postgresql://[::1]/db",
        "postgresql+asyncpg://u:secret@localhost:5575/db?sslmode=disable",
        "POSTGRESQL://LOCALHOST:5432/db",
        "pg0://disposable:5575?max_connections=300",
        "pg0://disposable",
        "pg0",
        "postgresql://localhost:5556/test",
        "postgresql:///db?host=localhost:5575&host=[::1]",
    ],
)
def test_allows_non_production_urls(guard, url):
    guard.assert_safe_database_url(url)


def test_additional_forbidden_ports(guard, monkeypatch):
    monkeypatch.setenv("HINDSIGHT_TEST_FORBIDDEN_DB_PORTS", "5436, 5556,5577")
    for port in (5436, 5556, 5577):
        with pytest.raises(ValueError, match=str(port)):
            guard.assert_safe_database_url(f"postgresql://localhost:{port}/db")


@pytest.mark.parametrize("denylist", ["", "5577"])
def test_production_port_cannot_be_removed(guard, monkeypatch, denylist):
    monkeypatch.setenv("HINDSIGHT_TEST_FORBIDDEN_DB_PORTS", denylist)
    with pytest.raises(ValueError, match="5436"):
        guard.assert_safe_database_url("postgresql://localhost:5436/db")


@pytest.mark.parametrize("denylist", ["not-a-port", "70000", "0"])
def test_invalid_denylist_fails_closed(guard, monkeypatch, denylist):
    monkeypatch.setenv("HINDSIGHT_TEST_FORBIDDEN_DB_PORTS", denylist)
    with pytest.raises(ValueError, match="HINDSIGHT_TEST_FORBIDDEN_DB_PORTS"):
        guard.assert_safe_database_url("postgresql://localhost:5575/db")


def test_malformed_url_does_not_leak_credentials(guard):
    with pytest.raises(ValueError) as exc:
        guard.assert_safe_database_url("postgresql://user:very-secret@[broken:5436/db")
    assert "very-secret" not in str(exc.value)


@pytest.mark.parametrize(
    "name", ["HINDSIGHT_API_DATABASE_URL", "HINDSIGHT_API_READ_DATABASE_URL", "HINDSIGHT_API_MIGRATION_DATABASE_URL"]
)
def test_environment_endpoints_refused(guard, monkeypatch, name):
    monkeypatch.setenv(name, "postgresql://u:secret@localhost:5436/db")
    with pytest.raises(ValueError, match=name):
        guard.check_test_database_environment()


def test_test_pg0_port_refused(guard, monkeypatch):
    monkeypatch.setenv("HINDSIGHT_TEST_PG_PORT", "5436")
    with pytest.raises(ValueError, match="HINDSIGHT_TEST_PG_PORT"):
        guard.check_test_database_environment()


def subprocess_environment():
    env = os.environ.copy()
    for name in list(env):
        if name.startswith("HINDSIGHT_API_"):
            env.pop(name)
    env.pop("HINDSIGHT_TEST_FORBIDDEN_DB_PORTS", None)
    env["HINDSIGHT_TEST_PG_PORT"] = "5575"
    env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    return env


@pytest.mark.parametrize("api_direct", [False, True])
@pytest.mark.parametrize("query_host", [False, True])
def test_pytest_aborts_before_collection_or_connection(api_direct, query_host):
    env = subprocess_environment()
    env["HINDSIGHT_API_DATABASE_URL"] = "postgresql://u:fake-password@127.0.0.1:5436/hindsight"
    if query_host:
        env["HINDSIGHT_API_DATABASE_URL"] = "postgresql:///hindsight?host=localhost:5436"
    # Instrument the child, so any attempted Python socket connection is evidence
    # of a regression rather than an actual connection to a protected instance.
    probe = """
import sys
import socket
import psycopg2
import asyncpg
import pytest

def no_connect(*args, **kwargs):
    print('CONNECTION_ATTEMPT', file=sys.stderr)
    raise AssertionError('CONNECTION_ATTEMPT')

def audit(event, args):
    if event == 'socket.connect':
        no_connect()
sys.addaudithook(audit)
socket.socket.connect = no_connect
socket.socket.connect_ex = no_connect
psycopg2.connect = no_connect
asyncpg.connect = no_connect
asyncpg.connection.connect = no_connect
raise SystemExit(pytest.main(['--collect-only', '-q', '-o', 'addopts=', 'tests/ci/test_test_db_guard.py']))
"""
    if api_direct:
        probe = probe.replace(
            "'tests/ci/test_test_db_guard.py'",
            "'--confcutdir=hindsight-api-slim/tests', 'hindsight-api-slim/tests/test_db_url.py'",
        )
    result = subprocess.run([sys.executable, "-c", probe], cwd=ROOT, env=env, capture_output=True, text=True)
    output = result.stdout + result.stderr
    assert result.returncode in (2, 4), output  # configure exit or early conftest import rejection
    assert "HINDSIGHT_API_DATABASE_URL" in output
    assert "[REDACTED]" in output
    assert "fake-password" not in output
    assert "CONNECTION_ATTEMPT" not in output
    assert "tests collected" not in output


@pytest.mark.parametrize("profile", ["preflight", "gate", "nightly"])
@pytest.mark.parametrize("query_host", [False, True])
def test_ci_refuses_before_running_any_stage(tmp_path, profile, query_host):
    env = subprocess_environment()
    env["HINDSIGHT_API_DATABASE_URL"] = "postgresql://u:fake-password@localhost:5436/hindsight"
    if query_host:
        env["HINDSIGHT_API_DATABASE_URL"] = "postgresql:///hindsight?host=localhost:5436"
    marker = tmp_path / "uv-called"
    fake_uv = tmp_path / "uv"
    fake_uv.write_text(f"#!/bin/sh\ntouch '{marker}'\nexit 95\n")
    fake_uv.chmod(0o755)
    env["PATH"] = f"{tmp_path}:{env['PATH']}"
    args = ["bash", "bin/ci", profile]
    if profile != "preflight":
        args.append(subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip())
    result = subprocess.run(args, cwd=ROOT, env=env, capture_output=True, text=True)
    output = result.stdout + result.stderr
    assert result.returncode == 2, output
    assert "HINDSIGHT_API_DATABASE_URL" in output
    assert "fake-password" not in output
    assert not marker.exists(), output
