"""Test-only database guards; URL/environment checks use only the stdlib."""

from __future__ import annotations

import os
import sys
from typing import TYPE_CHECKING
from urllib.parse import parse_qs, urlsplit

if TYPE_CHECKING:
    import pytest

FORBIDDEN_PORTS_ENV = "HINDSIGHT_TEST_FORBIDDEN_DB_PORTS"


def assert_safe_database_url(url: str | None, *, source: str = "HINDSIGHT_API_DATABASE_URL") -> None:
    """Reject protected ports without connecting or exposing URL credentials.

    The opt-in denylist can protect additional instances, but cannot disable the
    production and protected-test-port safeguards. Query-string ports also override libpq URL ports.
    """
    try:
        forbidden = {5436, 5556} | {
            int(value.strip()) for value in os.environ.get(FORBIDDEN_PORTS_ENV, "5436").split(",") if value.strip()
        }
        if any(port < 1 or port > 65535 for port in forbidden):
            raise ValueError
    except ValueError:
        raise ValueError(f"Invalid {FORBIDDEN_PORTS_ENV}: expected comma-separated ports from 1 to 65535.") from None

    if not url:
        return
    try:
        parsed = urlsplit(url)
        ports = {parsed.port if parsed.port is not None else 5432}
        query = parse_qs(parsed.query)
        for value in query.get("port", []):
            ports.update(int(port) for port in value.split(","))
        # SQLAlchemy/libpq also accept query hosts with embedded ports, including
        # repeated hosts and comma-separated failover endpoints. Check all of them.
        for value in query.get("host", []):
            for host in value.split(","):
                if ":" in host and not (host.startswith("[") and host.endswith("]")):
                    ports.add(int(host.rsplit(":", 1)[1] or "5432"))
        if any(port < 1 or port > 65535 for port in ports):
            raise ValueError
    except ValueError:
        # Never include the URL or parser exception: either can contain secrets,
        # including passwords embedded in query parameters or malformed netlocs.
        raise ValueError(f"Invalid {source} URL [REDACTED]; point it at a disposable database.") from None
    if query.get("service") or query.get("servicefile"):
        raise ValueError(f"Refusing test database from {source} URL [REDACTED]: service files hide endpoints.")
    denied = ports & forbidden
    if denied:
        raise ValueError(
            f"Refusing test database from {source} URL [REDACTED]: forbidden port(s) "
            f"{', '.join(str(port) for port in sorted(denied))} ({FORBIDDEN_PORTS_ENV}; production port 5436 "
            "and protected test port 5556 are always forbidden). Unset HINDSIGHT_API_DATABASE_URL "
            "or point it at a disposable database."
        )


def assert_safe_database_parameters(
    *, port: object = None, host: object = None, service: object = None, source: str = "connection parameters"
) -> None:
    """Validate programmatic/fallback parameters without resolving an endpoint."""
    if service:
        raise ValueError(
            f"Refusing test database from {source} [REDACTED]: service files can hide protected endpoints."
        )
    if port is not None:
        ports = port if isinstance(port, (list, tuple)) else [port]
        for value in ports:
            assert_safe_database_url(f"postgresql:///test?port={value}", source=source)
    hosts = host if isinstance(host, (list, tuple)) else [host]
    for value in hosts:
        if isinstance(value, (str, bytes)):
            name = os.fsdecode(value).rsplit("/", 1)[-1]
            if name.startswith(".s.PGSQL."):
                assert_safe_database_url(f"postgresql:///test?port={name.removeprefix('.s.PGSQL.')}", source=source)


def install_startup_guards(config: pytest.Config) -> None:
    """Refuse resolved endpoints before model/native work or isolated child dispatch.

    Install only when the optional API package is present; client-only pytest
    environments must not acquire API dependencies to enforce driver guards.
    Both root and direct-API scopes share this implementation and cleanup.
    """
    import importlib.util
    import inspect
    from functools import wraps

    import pytest

    if getattr(config, "_hindsight_startup_guards_installed", False):
        return
    if importlib.util.find_spec("hindsight_api") is None:
        return

    from hindsight_api import MemoryEngine, migrations
    from hindsight_api import config as api_config

    patches = pytest.MonkeyPatch()
    config.add_cleanup(patches.undo)

    @wraps(MemoryEngine.initialize)
    async def safe_initialize(engine):
        check_test_database_environment()
        assert_safe_database_url(engine.db_url)
        resolved = api_config.get_config()
        assert_safe_database_url(resolved.read_database_url, source="read_database_url")
        assert_safe_database_url(resolved.migration_database_url, source="migration_database_url")
        return await original_initialize(engine)

    original_initialize = MemoryEngine.initialize
    patches.setattr(MemoryEngine, "initialize", safe_initialize)

    def guard_migration(entry):
        signature = inspect.signature(entry)

        @wraps(entry)
        def safe_migration(*args, **kwargs):
            # A child interpreter does not inherit pytest's Python hooks. Check
            # both resolved endpoints in its parent, before native/child work.
            arguments = signature.bind(*args, **kwargs).arguments
            check_test_database_environment()
            assert_safe_database_url(arguments.get("database_url"))
            assert_safe_database_url(arguments.get("migration_database_url"), source="migration_database_url")
            return entry(*args, **kwargs)

        return safe_migration

    for name in (
        "run_migrations",
        "run_migrations_for_schemas",
        "ensure_embedding_dimension",
        "ensure_vector_extension",
        "ensure_text_search_extension",
    ):
        patches.setattr(migrations, name, guard_migration(getattr(migrations, name)))
    config._hindsight_startup_guards_installed = True


def install_driver_guards(config: pytest.Config) -> None:
    """Protect every pytest scope, including native libpq, without patching IPC."""
    import importlib.util
    from functools import wraps

    import pytest

    patches = pytest.MonkeyPatch()
    active = True

    def cleanup():
        nonlocal active
        active = False
        patches.undo()

    config.add_cleanup(cleanup)
    if importlib.util.find_spec("psycopg2") is not None:
        import psycopg2

        original = psycopg2.connect

        @wraps(original)
        def safe_connect(dsn=None, *args, **kwargs):
            check_test_database_environment()
            try:
                params = psycopg2.extensions.parse_dsn(dsn) if dsn else {}
            except psycopg2.ProgrammingError:
                raise ValueError("Invalid psycopg2 DSN [REDACTED]; point it at a disposable database.") from None
            # psycopg2.make_dsn drops None-valued kwargs; they do not override
            # fields already present in the DSN. Mirror that before validation.
            params.update({name: value for name, value in kwargs.items() if value is not None})
            assert_safe_database_parameters(
                port=params.get("port"), host=params.get("host"), service=params.get("service"), source="psycopg2"
            )
            return original(dsn, *args, **kwargs)

        patches.setattr(psycopg2, "connect", safe_connect)
    if importlib.util.find_spec("asyncpg") is not None:
        import asyncpg

        original_async = asyncpg.connection.connect

        @wraps(original_async)
        async def safe_async_connect(dsn=None, *args, **kwargs):
            check_test_database_environment()
            assert_safe_database_url(dsn, source="asyncpg")
            assert_safe_database_parameters(port=kwargs.get("port"), host=kwargs.get("host"), source="asyncpg")
            return await original_async(dsn, *args, **kwargs)

        patches.setattr(asyncpg, "connect", safe_async_connect)
        patches.setattr(asyncpg.connection, "connect", safe_async_connect)

    def socket_guard(event, args):
        if event != "socket.connect" or not active:
            return
        address = args[1]
        # Do not revalidate env on unrelated worker IPC. Only this actual socket
        # endpoint matters here; driver/startup guards own URL and env policy.
        if isinstance(address, tuple):
            assert_safe_database_parameters(port=address[1], source="socket")
        elif isinstance(address, (str, bytes)):
            assert_safe_database_parameters(host=address, source="socket")

    sys.addaudithook(socket_guard)


def check_test_database_environment() -> None:
    """Validate inherited endpoints, including the fallback pg0 port."""
    env = os.environ
    # libpq and asyncpg inherit these when the URL omits connection fields. An
    # isolated Alembic child has no pytest hooks, so reject them in its parent.
    assert_safe_database_parameters(port=env.get("PGPORT"), host=env.get("PGHOST"), source="PG*")
    for name in ("PGSERVICE", "PGSERVICEFILE"):
        if env.get(name):
            raise ValueError(
                f"Refusing test database from {name} [REDACTED]: service files can hide protected endpoints."
            )
    for name in (
        "HINDSIGHT_API_DATABASE_URL",
        "HINDSIGHT_API_READ_DATABASE_URL",
        "HINDSIGHT_API_MIGRATION_DATABASE_URL",
    ):
        assert_safe_database_url(env.get(name), source=name)
    assert_safe_database_url(f"pg0://test:{env.get('HINDSIGHT_TEST_PG_PORT', '5557')}", source="HINDSIGHT_TEST_PG_PORT")


if __name__ == "__main__":
    try:
        check_test_database_environment()
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(2) from None
