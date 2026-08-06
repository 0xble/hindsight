"""The superseded-by-coding-agents notice."""

from lib.upgrade_notice import DOCS_URL, upgrade_notice


def test_shown_on_every_session():
    """No cap and no interval — a notice shown once is a notice that gets missed."""
    for _ in range(5):
        assert upgrade_notice({}) is not None


def test_names_this_plugin_and_its_install_command():
    notice = upgrade_notice({})
    assert "install codex" in notice
    assert "@vectorize-io/hindsight-coding-agents" in notice


def test_links_the_docs_page():
    assert DOCS_URL in upgrade_notice({})
    assert DOCS_URL == "https://hindsight.vectorize.io/sdks/integrations/coding-agents"


def test_opt_out_silences_it():
    """The only thing making an every-session notice acceptable — so it must work."""
    assert upgrade_notice({"upgradeNotice": False}) is None


def test_opt_out_is_discoverable_from_the_message_itself():
    assert "upgradeNotice" in upgrade_notice({})


def test_never_raises():
    """A promotional message must never be why someone's session breaks."""

    class Hostile:
        def get(self, *a, **k):
            raise RuntimeError("boom")

    assert upgrade_notice(Hostile()) is None
