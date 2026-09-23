"""Opt-in fixture inputs and no-skip enforcement for the maintained CI lane."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import pytest

from scripts.ci.models import MODELS


@dataclass(frozen=True)
class ModelPaths:
    embedding: str
    reranker: str


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption("--portable-ci", action="store_true", help="Require isolated maintained-gate prerequisites")


@pytest.fixture(scope="session")
def portable_database() -> str:
    from hindsight_api.migrations import run_migrations

    url = os.environ["HINDSIGHT_CI_DATABASE_URL"]
    run_migrations(url)
    return url


@pytest.fixture(scope="session")
def portable_models() -> ModelPaths:
    root = Path(os.environ["HINDSIGHT_CI_MODELS"])
    return ModelPaths(**{model.name: str(root / model.name / model.revision) for model in MODELS})


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    if session.config.getoption("portable_ci"):
        reporter = session.config.pluginmanager.get_plugin("terminalreporter")
        if reporter is not None and reporter.stats.get("skipped"):
            reporter.write_sep("=", "Maintained CI coverage was skipped", red=True)
            session.exitstatus = pytest.ExitCode.TESTS_FAILED
