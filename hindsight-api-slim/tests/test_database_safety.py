"""Test-session default and resolved-URL regressions; no real DB connections."""

import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch
from urllib.parse import urlsplit

import psycopg2
import pytest
from hindsight_api.config import get_config
from hindsight_api.pg0 import EmbeddedPostgres, parse_pg0_url


def test_config_default_uses_test_pg0(monkeypatch):
    monkeypatch.delenv("HINDSIGHT_API_DATABASE_URL", raising=False)
    parsed = parse_pg0_url(get_config().database_url)
    assert parsed.instance_name != "hindsight"
    assert parsed.port == int(os.environ.get("HINDSIGHT_TEST_PG_PORT", "5557"))


@pytest.mark.parametrize("url", ["pg0", "pg0://hindsight"])
def test_bare_pg0_does_not_reuse_live_instance(url):
    parsed = parse_pg0_url(url)
    embedded = EmbeddedPostgres(name=parsed.instance_name, port=parsed.port)
    with patch("pg0.Pg0") as pg0:
        embedded._get_pg0()
    assert pg0.call_args.kwargs["name"] != "hindsight"
    assert pg0.call_args.kwargs["port"] == int(os.environ.get("HINDSIGHT_TEST_PG_PORT", "5557"))


@pytest.mark.asyncio
async def test_resolved_pg0_receipt_refused_before_caller_can_migrate(monkeypatch):
    embedded = EmbeddedPostgres(name="disposable", port=5575)
    monkeypatch.setattr(embedded, "is_running", AsyncMock(return_value=True))
    monkeypatch.setattr(embedded, "get_uri", AsyncMock(return_value="postgresql://u:secret@127.0.0.1:5436/db"))
    with pytest.raises(ValueError, match="5436") as exc:
        await embedded.ensure_running()
    assert "secret" not in str(exc.value)
    assert "[REDACTED]" in str(exc.value)


@pytest.mark.parametrize(
    "dsn",
    [
        "host=127.0.0.1 port=5436 user=u password=secret dbname=db",
        "host=/tmp port=5436 user=u dbname=db",
        "host=/var/run/postgresql port=5556 user=u dbname=db",
    ],
)
def test_libpq_refused_before_native_connect(monkeypatch, dsn):
    # psycopg2 bypasses Python socket audit events. Its C entrypoint must stay untouched.
    native = AsyncMock(side_effect=AssertionError("native connection reached"))
    monkeypatch.setattr(psycopg2, "_connect", native)
    with pytest.raises(ValueError, match="Refusing test database"):
        psycopg2.connect(dsn)
    native.assert_not_called()


def test_socket_refused_before_os_connect(request, monkeypatch):
    # Exercise the installed audit callback with a synthetic event, never an OS
    # connect (even against an unguarded base revision during RED).
    hooks = []
    monkeypatch.setattr(sys, "addaudithook", hooks.append)
    conftest = next(
        plugin
        for plugin in request.config.pluginmanager.get_plugins()
        if getattr(plugin, "__file__", None) == str(Path(__file__).with_name("conftest.py"))
    )
    conftest._install_database_connection_guards(request.config)
    with pytest.raises(ValueError, match="Refusing test database.*5436"):
        hooks[0]("socket.connect", (None, ("127.0.0.1", 5436)))


def test_configured_default_refused_before_session_connections(request, monkeypatch):
    from hindsight_api import config as api_config

    monkeypatch.setattr(api_config, "DEFAULT_DATABASE_URL", "postgresql://u:secret@127.0.0.1:5436/db")
    conftest = next(
        plugin
        for plugin in request.config.pluginmanager.get_plugins()
        if getattr(plugin, "__file__", None) == str(Path(__file__).with_name("conftest.py"))
    )
    with pytest.raises(pytest.UsageError, match="Refusing test database.*5436"):
        conftest.pytest_configure(request.config)


def test_named_pg0_without_port_also_avoids_the_implicit_allocator():
    with patch("pg0.Pg0") as pg0:
        EmbeddedPostgres(name="disposable", port=None)._get_pg0()
    port = pg0.call_args.kwargs["port"]
    assert isinstance(port, int), "Named test instances must not walk through protected ports either"
    assert port not in (5436, 5556)


def test_explicit_pg0_port_refused_before_instance_lookup():
    with patch("pg0.Pg0") as pg0:
        with pytest.raises(ValueError, match="Refusing test database.*5436"):
            EmbeddedPostgres(name="hindsight", port=5436)._get_pg0()
    pg0.assert_not_called()


@pytest.mark.asyncio
async def test_allowed_resolved_pg0_receipt(monkeypatch):
    embedded = EmbeddedPostgres(name="disposable", port=5575)
    monkeypatch.setattr(embedded, "is_running", AsyncMock(return_value=True))
    monkeypatch.setattr(embedded, "get_uri", AsyncMock(return_value="postgresql://127.0.0.1:5575/db"))
    assert urlsplit(await embedded.ensure_running()).port == 5575


@pytest.mark.asyncio
async def test_engine_without_db_url_refuses_default_before_native_migrations(monkeypatch):
    import sqlalchemy
    from hindsight_api import MemoryEngine, migrations
    from hindsight_api import config as api_config
    from hindsight_api.engine.task_backend import SyncTaskBackend

    # Restore the historical unsafe default, with no inherited API env. Every
    # native boundary is instrumented, so even RED cannot touch a real database.
    for name in list(os.environ):
        if name.startswith("HINDSIGHT_API_"):
            monkeypatch.delenv(name)
    assert not any(name.startswith("HINDSIGHT_API_") for name in os.environ)
    monkeypatch.setattr(api_config, "DEFAULT_DATABASE_URL", "postgresql://u@127.0.0.1:5436/db")
    api_config.clear_config_cache()
    native_attempts = []

    def no_native_connection(*args, **kwargs):
        native_attempts.append(args)
        raise AssertionError("Native migration connection attempted")

    monkeypatch.setattr(psycopg2, "connect", no_native_connection)
    monkeypatch.setattr(sqlalchemy, "create_engine", no_native_connection)
    monkeypatch.setattr(migrations, "create_engine", no_native_connection)

    class NoopModels:
        provider_name = "test"
        dimension = 384

        async def initialize(self):
            pass

        def load(self):
            pass

    engine = MemoryEngine(
        memory_llm_provider="none",
        memory_llm_model="none",
        embeddings=NoopModels(),
        cross_encoder=NoopModels(),
        query_analyzer=NoopModels(),
        task_backend=SyncTaskBackend(),
        skip_llm_verification=True,
    )
    assert urlsplit(engine.db_url).port == 5436
    try:
        with pytest.raises(ValueError, match="Refusing test database.*5436"):
            await engine.initialize()
    finally:
        assert native_attempts == [], "Refuse the resolved URL before SQLAlchemy/libpq, not only asyncpg"


@pytest.mark.parametrize(
    "entry,args",
    [
        ("run_migrations", []),
        ("run_migrations_for_schemas", [["public"]]),
        ("ensure_embedding_dimension", [384]),
        ("ensure_vector_extension", ["pgvector"]),
        ("ensure_text_search_extension", ["native"]),
    ],
)
def test_migration_entries_refuse_before_native_or_child_work(monkeypatch, entry, args):
    from hindsight_api import migrations

    attempts = []

    def no_native_or_child_work(*args, **kwargs):
        attempts.append(args)
        raise AssertionError("Native engine or migration subprocess reached")

    monkeypatch.setattr(migrations, "create_engine", no_native_or_child_work)
    monkeypatch.setattr(migrations, "_run_in_migration_child", no_native_or_child_work)
    monkeypatch.setattr(migrations, "_should_isolate_migrations", lambda: True)
    try:
        with pytest.raises(ValueError, match="Refusing test database.*5436"):
            getattr(migrations, entry)("postgresql://u@127.0.0.1:5436/db", *args)
    finally:
        assert attempts == []


@pytest.mark.asyncio
async def test_connection_refusal_is_a_normal_error_not_a_worker_exit(monkeypatch):
    embedded = EmbeddedPostgres(name="disposable", port=5575)
    monkeypatch.setattr(embedded, "is_running", AsyncMock(return_value=True))
    monkeypatch.setattr(embedded, "get_uri", AsyncMock(return_value="postgresql://localhost:5436/db"))
    with pytest.raises(ValueError, match="Refusing test database.*5436"):
        await embedded.ensure_running()


def test_xdist_pg0_allocates_an_explicit_safe_port(request, monkeypatch, tmp_path):
    """pg0's implicit allocator starts at 5432 and walks through protected 5436."""
    from types import SimpleNamespace

    import hindsight_api.migrations

    conftest = next(
        plugin
        for plugin in request.config.pluginmanager.get_plugins()
        if getattr(plugin, "__file__", None) == str(Path(__file__).with_name("conftest.py"))
    )
    captured = {}

    def fake_pg0(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(
            ensure_running=AsyncMock(return_value="postgresql://localhost:5575/db"), drop=AsyncMock()
        )

    monkeypatch.setattr(conftest, "EmbeddedPostgres", fake_pg0)
    monkeypatch.setattr(conftest, "_cleanup_stale_test_data", lambda url: None)
    monkeypatch.setattr(hindsight_api.migrations, "run_migrations", lambda url: None)
    worker_dir = tmp_path / "gw0"
    worker_dir.mkdir()
    fixture = conftest.pg0_db_url.__wrapped__(None, SimpleNamespace(getbasetemp=lambda: worker_dir), "gw0")
    next(fixture)
    try:
        assert isinstance(captured["port"], int), "pg0 must not implicitly allocate a protected port"
        conftest._db_guard.assert_safe_database_url(f"pg0://test:{captured['port']}")
    finally:
        fixture.close()


@pytest.mark.parametrize("worker_id", ["gw0", "master"])
@pytest.mark.parametrize("failure_stage", [None, "startup", "migration", "test"])
def test_pg0_fixture_drops_only_worker_owned_instances(request, monkeypatch, tmp_path, worker_id, failure_stage):
    """Worker data is disposable even on setup/test failure; serial data persists."""
    from types import SimpleNamespace
    from unittest.mock import Mock

    import hindsight_api.migrations

    conftest = next(
        plugin
        for plugin in request.config.pluginmanager.get_plugins()
        if getattr(plugin, "__file__", None) == str(Path(__file__).with_name("conftest.py"))
    )
    postgres = SimpleNamespace(
        ensure_running=AsyncMock(return_value="postgresql://localhost:5575/db"),
        stop=AsyncMock(),
        drop=AsyncMock(),
    )
    if failure_stage == "startup":
        postgres.ensure_running.side_effect = RuntimeError("simulated startup failure")
    migrations = Mock()
    if failure_stage == "migration":
        migrations.side_effect = RuntimeError("simulated migration failure")
    constructor = Mock(return_value=postgres)
    monkeypatch.setattr(conftest, "EmbeddedPostgres", constructor)
    monkeypatch.setattr(conftest, "_cleanup_stale_test_data", lambda url: None)
    monkeypatch.setattr(hindsight_api.migrations, "run_migrations", migrations)
    worker_dir = tmp_path / "gw0"
    worker_dir.mkdir()
    fixture = conftest.pg0_db_url.__wrapped__(
        "pg0://fixture-test:5575", SimpleNamespace(getbasetemp=lambda: worker_dir), worker_id
    )
    if failure_stage in ("startup", "migration"):
        with pytest.raises(RuntimeError, match=f"simulated {failure_stage} failure"):
            next(fixture)
    else:
        assert next(fixture) == "postgresql://localhost:5575/db"
        if failure_stage == "test":
            with pytest.raises(AssertionError, match="simulated test failure"):
                fixture.throw(AssertionError("simulated test failure"))
        else:
            fixture.close()
    if worker_id == "master":
        assert constructor.call_args.kwargs["name"] == "fixture-test"
        postgres.drop.assert_not_awaited()
    else:
        assert constructor.call_args.kwargs["name"] == f"fixture-test-{tmp_path.name}-gw0"
        postgres.drop.assert_awaited_once_with()
    postgres.stop.assert_not_awaited()


@pytest.mark.parametrize("port", [5436, 5556])
@pytest.mark.parametrize("as_bytes", [False, True])
def test_unix_socket_refused_before_os_connect(request, monkeypatch, port, as_bytes):
    hooks = []
    monkeypatch.setattr(sys, "addaudithook", hooks.append)
    conftest = next(
        plugin
        for plugin in request.config.pluginmanager.get_plugins()
        if getattr(plugin, "__file__", None) == str(Path(__file__).with_name("conftest.py"))
    )
    conftest._install_database_connection_guards(request.config)
    address = f"/var/run/postgresql/.s.PGSQL.{port}"
    if as_bytes:
        address = address.encode()
    with pytest.raises(ValueError, match=f"Refusing test database.*{port}"):
        hooks[0]("socket.connect", (None, address))


@pytest.mark.parametrize("port", [5436, 5556])
def test_actual_unix_socket_connect_is_refused(port):
    import socket
    import tempfile

    # This owned listener is not a DB. A short scratch path fits AF_UNIX's path limit.
    with tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR"), prefix="pg-") as directory:
        address = str(Path(directory) / f".s.PGSQL.{port}")
        with socket.socket(socket.AF_UNIX) as listener, socket.socket(socket.AF_UNIX) as client:
            listener.bind(address)
            listener.listen()
            with pytest.raises(ValueError, match=f"Refusing test database.*{port}"):
                client.connect(address)


@pytest.mark.asyncio
async def test_no_db_url_with_libpq_fallback_refused_before_alembic(monkeypatch):
    from hindsight_api import MemoryEngine, migrations
    from hindsight_api import config as api_config
    from hindsight_api.engine.task_backend import SyncTaskBackend

    # No db_url, and no API env: libpq's native fallback alone selects production.
    for name in list(os.environ):
        if name.startswith(("HINDSIGHT_API_", "PG")):
            monkeypatch.delenv(name)
    monkeypatch.setattr(api_config, "DEFAULT_DATABASE_URL", "postgresql:///fake_db?host=/nonexistent/pr59")
    api_config.clear_config_cache()
    monkeypatch.setenv("PGPORT", "5436")
    monkeypatch.setenv("PGHOST", "/nonexistent/pr59")
    native = AsyncMock(side_effect=AssertionError("native libpq reached"))
    monkeypatch.setattr(psycopg2, "_connect", native)
    models = AsyncMock()
    models.provider_name = "test"
    models.dimension = 384
    engine = MemoryEngine(
        memory_llm_provider="none",
        memory_llm_model="none",
        embeddings=models,
        cross_encoder=models,
        query_analyzer=models,
        task_backend=SyncTaskBackend(),
        skip_llm_verification=True,
    )
    migration = AsyncMock(side_effect=AssertionError("startup migration reached"))
    monkeypatch.setattr(migrations, "run_migrations_for_schemas", migration)
    with pytest.raises(ValueError, match="Refusing test database.*5436"):
        await engine.initialize()
    migration.assert_not_called()
    native.assert_not_called()
    models.initialize.assert_not_called()


@pytest.mark.parametrize(
    "entry,args",
    [
        ("run_migrations", []),
        ("run_migrations_for_schemas", [["public"]]),
        ("ensure_embedding_dimension", [384]),
        ("ensure_vector_extension", ["pgvector"]),
        ("ensure_text_search_extension", ["native"]),
    ],
)
def test_native_migration_refuses_omitted_port_before_child_or_sqlalchemy(monkeypatch, entry, args):
    from hindsight_api import migrations

    monkeypatch.setenv("PGHOST", "/nonexistent/pr59")
    monkeypatch.setenv("PGPORT", "5436")
    native = AsyncMock(side_effect=AssertionError("native SQLAlchemy reached"))
    child = AsyncMock(side_effect=AssertionError("migration child reached"))
    monkeypatch.setattr(migrations, "create_engine", native)
    monkeypatch.setattr(migrations, "_run_in_migration_child", child)
    monkeypatch.setattr(migrations, "_should_isolate_migrations", lambda: True)
    with pytest.raises(ValueError, match="Refusing test database.*5436"):
        getattr(migrations, entry)("postgresql:///fake_db?host=/nonexistent/pr59", *args)
    native.assert_not_called()
    child.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kwargs",
    [
        {"host": "/nonexistent/pr59", "port": 5436},
        {"host": ["/nonexistent/pr59", "127.0.0.1"], "port": [5575, 5436]},
    ],
)
async def test_programmatic_asyncpg_refused_before_resolver(monkeypatch, kwargs):
    import asyncpg

    resolver = AsyncMock(side_effect=AssertionError("asyncpg resolver reached"))
    monkeypatch.setattr(asyncpg.connect_utils, "_connect", resolver)
    with pytest.raises(ValueError, match="Refusing test database.*5436"):
        await asyncpg.connect(**kwargs)
    resolver.assert_not_called()


def test_psycopg2_service_cannot_hide_native_endpoint(monkeypatch):
    native = AsyncMock(side_effect=AssertionError("native libpq reached"))
    monkeypatch.setattr(psycopg2, "_connect", native)
    with pytest.raises(ValueError, match="Refusing test database"):
        psycopg2.connect(service="fake_production")
    native.assert_not_called()
