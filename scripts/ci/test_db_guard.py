"""Test-only database safety checks shared by pytest and bin/ci (stdlib only)."""

from __future__ import annotations

import os
import sys
from urllib.parse import parse_qs, urlsplit

FORBIDDEN_PORTS_ENV = "HINDSIGHT_TEST_FORBIDDEN_DB_PORTS"


def assert_safe_database_url(url: str | None, *, source: str = "HINDSIGHT_API_DATABASE_URL") -> None:
    """Reject protected ports without connecting or exposing URL credentials.

    The opt-in denylist can protect additional instances, but cannot disable the
    production-port safeguard. Query-string ports also override libpq URL ports.
    """
    try:
        forbidden = {5436} | {
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
    denied = ports & forbidden
    if denied:
        raise ValueError(
            f"Refusing test database from {source} URL [REDACTED]: forbidden port(s) "
            f"{', '.join(str(port) for port in sorted(denied))} ({FORBIDDEN_PORTS_ENV}; production port 5436 "
            "is always forbidden). Unset HINDSIGHT_API_DATABASE_URL or point it at a disposable database."
        )


def check_test_database_environment() -> None:
    """Validate inherited endpoints, including the fallback pg0 port."""
    env = os.environ
    for name in (
        "HINDSIGHT_API_DATABASE_URL",
        "HINDSIGHT_API_READ_DATABASE_URL",
        "HINDSIGHT_API_MIGRATION_DATABASE_URL",
    ):
        assert_safe_database_url(env.get(name), source=name)
    assert_safe_database_url(f"pg0://test:{env.get('HINDSIGHT_TEST_PG_PORT', '5556')}", source="HINDSIGHT_TEST_PG_PORT")


if __name__ == "__main__":
    try:
        check_test_database_environment()
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(2) from None
