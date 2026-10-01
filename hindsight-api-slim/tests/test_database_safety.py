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
    assert parsed.port == int(os.environ.get("HINDSIGHT_TEST_PG_PORT", "5556"))


@pytest.mark.parametrize("url", ["pg0", "pg0://hindsight"])
def test_bare_pg0_does_not_reuse_live_instance(url):
    parsed = parse_pg0_url(url)
    embedded = EmbeddedPostgres(name=parsed.instance_name, port=parsed.port)
    with patch("pg0.Pg0") as pg0:
        embedded._get_pg0()
    assert pg0.call_args.kwargs["name"] != "hindsight"
    assert pg0.call_args.kwargs["port"] == int(os.environ.get("HINDSIGHT_TEST_PG_PORT", "5556"))


@pytest.mark.asyncio
async def test_resolved_pg0_receipt_refused_before_caller_can_migrate(monkeypatch):
    embedded = EmbeddedPostgres(name="disposable", port=5575)
    monkeypatch.setattr(embedded, "is_running", AsyncMock(return_value=True))
    monkeypatch.setattr(embedded, "get_uri", AsyncMock(return_value="postgresql://u:secret@127.0.0.1:5436/db"))
    with pytest.raises(pytest.exit.Exception, match="5436") as exc:
        await embedded.ensure_running()
    assert "secret" not in str(exc.value)
    assert "[REDACTED]" in str(exc.value)


def test_libpq_refused_before_native_connect(monkeypatch):
    # psycopg2 bypasses Python socket audit events. Its C entrypoint must stay untouched.
    native = AsyncMock(side_effect=AssertionError("native connection reached"))
    monkeypatch.setattr(psycopg2, "_connect", native)
    with pytest.raises(pytest.exit.Exception, match="Refusing test database.*5436"):
        psycopg2.connect("host=127.0.0.1 port=5436 user=u password=secret dbname=db")
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
    with pytest.raises(pytest.exit.Exception, match="Refusing test database.*5436"):
        hooks[0]("socket.connect", (None, ("127.0.0.1", 5436)))


def test_configured_default_refused_before_session_connections(request, monkeypatch):
    from hindsight_api import config as api_config

    monkeypatch.setattr(api_config, "DEFAULT_DATABASE_URL", "postgresql://u:secret@127.0.0.1:5436/db")
    conftest = next(
        plugin
        for plugin in request.config.pluginmanager.get_plugins()
        if getattr(plugin, "__file__", None) == str(Path(__file__).with_name("conftest.py"))
    )
    with pytest.raises(pytest.exit.Exception, match="Refusing test database.*5436"):
        conftest.pytest_configure(request.config)


def test_explicit_pg0_port_refused_before_instance_lookup():
    with patch("pg0.Pg0") as pg0:
        with pytest.raises(pytest.exit.Exception, match="Refusing test database.*5436"):
            EmbeddedPostgres(name="hindsight", port=5436)._get_pg0()
    pg0.assert_not_called()


@pytest.mark.asyncio
async def test_allowed_resolved_pg0_receipt(monkeypatch):
    embedded = EmbeddedPostgres(name="disposable", port=5575)
    monkeypatch.setattr(embedded, "is_running", AsyncMock(return_value=True))
    monkeypatch.setattr(embedded, "get_uri", AsyncMock(return_value="postgresql://127.0.0.1:5575/db"))
    assert urlsplit(await embedded.ensure_running()).port == 5575
