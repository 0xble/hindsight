"""One-line, rate-limited notice that this plugin is superseded.

This plugin still works and still receives fixes, but development has moved to
`@vectorize-io/hindsight-coding-agents`. Someone who installed this a year ago has no reason to
ever learn that, so the session itself has to say it — a changelog entry reaches nobody.

Restraint is the whole design:

  * capped at MAX_SHOWINGS in total, never more often than every MIN_INTERVAL_DAYS, so it can't
    become a recurring interruption
  * one `upgradeNotice: false` in the user config silences it permanently, and the text says so
  * any failure — unreadable state, unwritable state dir — returns None rather than raising. A
    promotional message must never be the reason someone's session breaks.

Deliberately duplicated in the codex plugin rather than shared: the two ship as independent
packages with no common module path, and ~40 lines is cheaper than inventing one.
"""

from datetime import datetime, timedelta, timezone

from .state import read_state, write_state

STATE_FILE = "upgrade_notice.json"
MAX_SHOWINGS = 3
MIN_INTERVAL_DAYS = 7

NOTICE = (
    "💡 Hindsight: this Claude Code plugin is superseded by the Coding Agents plugin — one "
    "install covering Claude Code, Codex, Cursor, Copilot, opencode, Kilo, Grok, Antigravity, "
    "Devin and Cline, with a single memory per repo that every agent shares instead of one bank "
    "per agent.\n"
    "\n"
    "    npm install -g @vectorize-io/hindsight-coding-agents\n"
    "    hindsight-coding-agents install claude-code --import-conversations\n"
    "\n"
    "Your Hindsight server and token carry over automatically; past conversations are re-imported "
    'from local transcripts. Silence this with "upgradeNotice": false in '
    "~/.hindsight/claude-code.json"
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def upgrade_notice(config: dict, now: datetime | None = None) -> str | None:
    """Return the notice to show this session, or None to stay quiet."""
    try:
        if not config.get("upgradeNotice", True):
            return None

        now = now or _now()
        state = read_state(STATE_FILE, {}) or {}
        shown = state.get("shown", 0)
        if not isinstance(shown, int) or shown >= MAX_SHOWINGS:
            return None

        last_raw = state.get("last")
        if last_raw:
            try:
                last = datetime.fromisoformat(str(last_raw))
                if last.tzinfo is None:
                    last = last.replace(tzinfo=timezone.utc)
                if now - last < timedelta(days=MIN_INTERVAL_DAYS):
                    return None
            except ValueError:
                pass  # unparseable timestamp: treat as never shown rather than nagging forever

        write_state(STATE_FILE, {"shown": shown + 1, "last": now.isoformat()})
        return NOTICE
    except Exception:
        return None
