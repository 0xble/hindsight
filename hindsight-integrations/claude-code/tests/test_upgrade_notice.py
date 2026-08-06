"""The superseded-by-coding-agents notice: shown, but strictly rate-limited."""

from datetime import datetime, timedelta, timezone

import pytest

from lib.upgrade_notice import MAX_SHOWINGS, MIN_INTERVAL_DAYS, upgrade_notice

NOW = datetime(2026, 8, 6, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _isolated_state(state_dir):
    """Never touch the developer's real state files."""
    return state_dir


def test_shows_on_a_fresh_install():
    assert upgrade_notice({}, now=NOW) is not None


def test_names_this_plugin_and_its_install_command():
    notice = upgrade_notice({}, now=NOW)
    assert "install claude-code" in notice
    assert "@vectorize-io/hindsight-coding-agents" in notice
    # The opt-out has to be discoverable from the message itself.
    assert "upgradeNotice" in notice


def test_opt_out_silences_it():
    assert upgrade_notice({"upgradeNotice": False}, now=NOW) is None


def test_not_repeated_within_the_interval():
    assert upgrade_notice({}, now=NOW) is not None
    assert upgrade_notice({}, now=NOW + timedelta(days=MIN_INTERVAL_DAYS - 1)) is None


def test_repeats_after_the_interval():
    assert upgrade_notice({}, now=NOW) is not None
    assert upgrade_notice({}, now=NOW + timedelta(days=MIN_INTERVAL_DAYS + 1)) is not None


def test_stops_for_good_after_the_cap():
    """Bounded on purpose — a deprecation notice must not become a permanent interruption."""
    now = NOW
    for _ in range(MAX_SHOWINGS):
        assert upgrade_notice({}, now=now) is not None
        now += timedelta(days=MIN_INTERVAL_DAYS + 1)
    assert upgrade_notice({}, now=now) is None
    assert upgrade_notice({}, now=now + timedelta(days=365)) is None


def test_unwritable_state_returns_none_rather_than_raising(monkeypatch):
    """A promotional message must never be why someone's session breaks."""
    import lib.upgrade_notice as mod

    monkeypatch.setattr(mod, "write_state", lambda *a, **k: (_ for _ in ()).throw(OSError("read-only")))
    assert upgrade_notice({}, now=NOW) is None


def test_corrupt_timestamp_does_not_wedge_it(monkeypatch):
    import lib.upgrade_notice as mod

    monkeypatch.setattr(mod, "read_state", lambda *a, **k: {"shown": 1, "last": "not-a-date"})
    assert upgrade_notice({}, now=NOW) is not None
