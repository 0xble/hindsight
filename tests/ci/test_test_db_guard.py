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
        "postgresql://localhost:5557/test",
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


@pytest.mark.parametrize("port", [5436, 5556])
@pytest.mark.parametrize("denylist", ["", "5577"])
def test_production_port_cannot_be_removed(guard, monkeypatch, denylist, port):
    monkeypatch.setenv("HINDSIGHT_TEST_FORBIDDEN_DB_PORTS", denylist)
    with pytest.raises(ValueError, match=str(port)):
        guard.assert_safe_database_url(f"postgresql://localhost:{port}/db")


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
        if name.startswith(("HINDSIGHT_API_", "PG")):
            env.pop(name)
    env.pop("HINDSIGHT_TEST_FORBIDDEN_DB_PORTS", None)
    env["HINDSIGHT_TEST_PG_PORT"] = "5575"
    env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    return env


@pytest.mark.parametrize("api_direct", [False, True])
@pytest.mark.parametrize("endpoint", ["url", "query_host", "libpq"])
def test_pytest_aborts_before_collection_or_connection(api_direct, endpoint):
    env = subprocess_environment()
    env["HINDSIGHT_API_DATABASE_URL"] = "postgresql://u:fake-password@127.0.0.1:5436/hindsight"
    if endpoint == "query_host":
        env["HINDSIGHT_API_DATABASE_URL"] = "postgresql:///hindsight?host=localhost:5436"
    elif endpoint == "libpq":
        env.pop("HINDSIGHT_API_DATABASE_URL")
        env.update(PGHOST="/nonexistent/pr59", PGPORT="5436")
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
    assert result.returncode == 4, output  # clear pytest usage error, not a worker exit
    assert ("PG*" if endpoint == "libpq" else "HINDSIGHT_API_DATABASE_URL") in output
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


@pytest.mark.parametrize("port", [5436, 5556])
@pytest.mark.parametrize("directory", ["/tmp", "/var/run/postgresql", "%2Ftmp"])
def test_socket_directory_dsn_refuses_protected_ports(guard, monkeypatch, port, directory):
    monkeypatch.setenv("HINDSIGHT_TEST_FORBIDDEN_DB_PORTS", "")
    with pytest.raises(ValueError, match=f"Refusing test database.*{port}"):
        guard.assert_safe_database_url(f"postgresql://u@/db?host={directory}&port={port}")


@pytest.mark.parametrize(
    "settings",
    [
        {"PGHOST": "/nonexistent/pr59", "PGPORT": "5436"},
        {"PGHOST": "127.0.0.1", "PGPORT": "5436"},
        {"PGSERVICE": "production"},
        {"PGSERVICEFILE": "/nonexistent/service.conf"},
    ],
)
def test_libpq_environment_refused_before_any_url_or_native_work(guard, monkeypatch, settings):
    for key, value in settings.items():
        monkeypatch.setenv(key, value)
    with pytest.raises(ValueError, match="Refusing test database"):
        guard.check_test_database_environment()


@pytest.mark.parametrize("profile", ["preflight", "gate", "nightly"])
def test_ci_refuses_inherited_libpq_fallback_before_stages(tmp_path, profile):
    env = subprocess_environment()
    env.update(PGHOST="/nonexistent/pr59", PGPORT="5436")
    marker = tmp_path / "uv-called"
    fake_uv = tmp_path / "uv"
    fake_uv.write_text(f"#!/bin/sh\ntouch '{marker}'\nexit 95\n")
    fake_uv.chmod(0o755)
    env["PATH"] = f"{tmp_path}:{env['PATH']}"
    args = ["bash", "bin/ci", profile]
    if profile != "preflight":
        args.append(subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip())
    result = subprocess.run(args, cwd=ROOT, env=env, capture_output=True, text=True)
    assert result.returncode == 2, result.stdout + result.stderr
    assert "Refusing test database" in result.stderr
    assert not marker.exists()


def test_root_native_driver_guard_refuses_programmatic_endpoint(monkeypatch):
    from unittest.mock import Mock

    psycopg2 = pytest.importorskip("psycopg2")
    native = Mock(side_effect=AssertionError("native libpq reached"))
    monkeypatch.setattr(psycopg2, "_connect", native)
    with pytest.raises(ValueError, match="Refusing test database.*5436"):
        psycopg2.connect(host="/nonexistent/pr59", port=5436)
    native.assert_not_called()


@pytest.mark.parametrize(
    "url",
    [
        "postgresql://dummy@127.0.0.1:5436/dummy",
        "postgresql:///dummy?host=/nonexistent/pr59-root&port=5436",
    ],
)
@pytest.mark.parametrize("migration_endpoint", [False, True])
def test_root_migration_isolation_refuses_before_child_dispatch(monkeypatch, url, migration_endpoint):
    from unittest.mock import Mock

    migrations = pytest.importorskip("hindsight_api.migrations")
    from hindsight_api.config import clear_config_cache

    # Root-scoped pytest never loads the API conftest. Exercise real isolation
    # configuration, stopping the child boundary even on an unguarded RED head.
    dispatched = Mock(side_effect=AssertionError("migration subprocess boundary reached"))
    monkeypatch.setenv("HINDSIGHT_API_MIGRATION_ISOLATION", "true")
    clear_config_cache()
    monkeypatch.setattr(migrations, "_run_in_migration_child", dispatched)
    try:
        with pytest.raises(ValueError, match="Refusing test database.*5436"):
            if migration_endpoint:
                migrations.run_migrations("postgresql://dummy@127.0.0.1:5575/dummy", migration_database_url=url)
            else:
                migrations.run_migrations(url)
        dispatched.assert_not_called()
    finally:
        clear_config_cache()


def test_root_migration_isolation_allows_disposable_child_dispatch(monkeypatch):
    from unittest.mock import Mock

    migrations = pytest.importorskip("hindsight_api.migrations")
    from hindsight_api.config import clear_config_cache

    dispatched = Mock()
    monkeypatch.setenv("HINDSIGHT_API_MIGRATION_ISOLATION", "true")
    clear_config_cache()
    monkeypatch.setattr(migrations, "_run_in_migration_child", dispatched)
    try:
        migrations.run_migrations("postgresql://dummy@127.0.0.1:5575/dummy")
        dispatched.assert_called_once()
        assert dispatched.call_args.args[0] == "run_migrations"
    finally:
        clear_config_cache()


@pytest.mark.parametrize("entry", ["export", "connection", "pool"])
def test_root_async_driver_guard_refuses_before_resolver(monkeypatch, entry):
    import asyncio
    from unittest.mock import AsyncMock

    asyncpg = pytest.importorskip("asyncpg")
    resolver = AsyncMock(side_effect=AssertionError("asyncpg resolver reached"))
    monkeypatch.setattr(asyncpg.connect_utils, "_connect", resolver)

    async def connect_to_fake_endpoint():
        with pytest.raises(ValueError, match="Refusing test database.*5436"):
            if entry == "pool":
                async with asyncpg.create_pool(host="/nonexistent/pr59", port=5436, min_size=1):
                    pass
            else:
                connect = asyncpg.connect if entry == "export" else asyncpg.connection.connect
                await connect(host="/nonexistent/pr59", port=5436)

    asyncio.run(connect_to_fake_endpoint())
    resolver.assert_not_called()


@pytest.mark.parametrize(
    "dsn,kwargs",
    [
        ("host=/nonexistent/pr59 port=5436 dbname=fake", {"port": None}),
        ("service=fake_production", {"service": None}),
    ],
)
def test_none_kwarg_does_not_erase_native_dsn_endpoint(monkeypatch, dsn, kwargs):
    from unittest.mock import Mock

    psycopg2 = pytest.importorskip("psycopg2")
    native = Mock(side_effect=AssertionError("native libpq reached"))
    monkeypatch.setattr(psycopg2, "_connect", native)
    with pytest.raises(ValueError, match="Refusing test database"):
        psycopg2.connect(dsn, **kwargs)
    native.assert_not_called()
