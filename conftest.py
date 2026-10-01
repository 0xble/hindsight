"""Repository-wide pytest safety boundary; never import application code here."""

import pytest

from scripts.ci.test_db_guard import check_test_database_environment, install_driver_guards


@pytest.hookimpl(tryfirst=True)
def pytest_configure(config: pytest.Config) -> None:
    # Reject inherited production settings before collection or fixture setup.
    # The API conftest repeats this after loading its workspace .env.
    try:
        check_test_database_environment()
    except ValueError as exc:
        raise pytest.UsageError(str(exc)) from None
    install_driver_guards(config)
