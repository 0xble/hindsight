"""Notice that this plugin is superseded, shown at every session start.

This plugin still works, but development has moved to
`@vectorize-io/hindsight-coding-agents`. Someone who installed this a year ago has no way to learn
that — a changelog entry reaches nobody — so the session itself says it.

Shown every session rather than a few times: a notice that appears once is missed, and this one has
a concrete action attached. `"upgradeNotice": false` in the user config turns it off permanently,
and the message says so, which is what keeps that acceptable.

Any failure returns None rather than raising. A promotional message must never be the reason
someone's session breaks.

Deliberately duplicated in the claude-code plugin rather than shared: the two ship as independent
packages with no common module path.
"""

DOCS_URL = "https://hindsight.vectorize.io/sdks/integrations/coding-agents"

NOTICE = (
    "💡 Hindsight: this Codex plugin is superseded by the Coding Agents plugin — one "
    "install covering Claude Code, Codex, Cursor, Copilot, opencode, Kilo, Grok, Antigravity, "
    "Devin and Cline, with a single memory per repo that every agent shares instead of one bank "
    "per agent.\n"
    "\n"
    "    npm install -g @vectorize-io/hindsight-coding-agents\n"
    "    hindsight-coding-agents install codex --import-conversations\n"
    "\n"
    f"Docs: {DOCS_URL}\n"
    "Your Hindsight server and token carry over automatically; past conversations are re-imported "
    'from local transcripts. Silence this with "upgradeNotice": false in '
    "~/.hindsight/codex.json"
)


def upgrade_notice(config: dict) -> str | None:
    """Return the notice, or None when the user has turned it off."""
    try:
        return None if not config.get("upgradeNotice", True) else NOTICE
    except Exception:
        return None
