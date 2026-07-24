# hindsight-memory-v2 (Claude Code plugin)

## What it is

Repo-scoped, long-term memory for Claude Code. The plugin resolves one
Hindsight memory bank per repository and keeps it in sync with your work:
every prompt gets relevant memory injected, every session's transcript gets
written back, new repos get offered a one-time background seed from git
history, and an MCP server exposes knowledge-page tools against the same
bank. This package (`hindsight-integrations/claude-code-v2/`) is a thin
wrapper — all the actual logic (recall/reflect, transcript parsing, seeding,
bank resolution, config loading) lives in the shared TypeScript core at
`../hindsight-coding-agents` and is bundled self-contained into `dist/` by
`scripts/build.mjs`.

## What you get

Four surfaces, all wired up automatically once the plugin is installed:

1. **Recall + reflect (`UserPromptSubmit` hook, `dist/claude-hook.js`)** —
   every prompt triggers a `recall` for relevant memory, injected as a
   `<hindsight_memories>` block (with a 🧠 attribution header) into
   `additionalContext`. On the first prompt of a session it also runs a
   deeper `reflect` pass. Fails open: if the Hindsight server isn't
   reachable, or anything errors, the hook emits no context and never
   breaks the turn.
2. **Live write-back (`Stop` hook, `dist/claude-stop-hook.js`)** — when a
   session ends, the transcript is retained into the repo's bank
   (`retainLiveSession`), so what happened in the session compounds into
   memory for next time. On by default unless the plugin is `disabled`.
3. **Auto-seed for new repos (`SessionStart` hook,
   `dist/claude-sessionstart-hook.js`)** — if the repo's bank looks cold (no
   git-sourced memory yet) and you haven't already seeded or declined, the
   agent is prompted to ask you whether to learn the repo's git history. If
   you say yes, it runs `dist/hindsight-seed.js seed --repo <dir>`, which
   kicks off a non-blocking background backfill (extraction happens
   server-side/async, so it doesn't consume your session's tokens or block
   your work).
4. **Knowledge-page tools (MCP server, `.mcp.json` → `dist/mcp-server.js`)**
   — exposes `agent_knowledge_*` tools (list/get/create/update/delete
   knowledge pages, plus recall) against the same per-repo bank the hooks
   use.

## Bank model

Banks are resolved per repo, dynamically, using `bankIdTemplate` (default
`{gitProject}`) — and this is worktree-aware, so different worktrees of the
same repo share a bank. All four surfaces (recall/reflect, write-back,
seeding, and the MCP knowledge-page tools) resolve and share the exact same
bank for a given repo.

## Install

**Requires a reachable Hindsight server** (default `http://localhost:8888`).

**Via marketplace** (once published): use `/plugin` in Claude Code and
install `hindsight-memory-v2` from the `hindsight` marketplace.

**Local dev install:**

```bash
node hindsight-integrations/claude-code-v2/scripts/build.mjs
bash hindsight-integrations/claude-code-v2/scripts/dev-install.sh
```

`dev-install.sh` builds the wrapper and copies it into
`~/.claude/plugins/cache/hindsight/hindsight-memory-v2/<version>/`. It never
edits `~/.claude/plugins/installed_plugins.json` or `~/.claude/settings.json`
itself — it prints the exact JSON snippets to add to each, and reminds you
to restart Claude Code afterward. Follow its printed steps.

⚠️ **Disable the v1 `hindsight-memory` plugin first.** Both plugins hook
`UserPromptSubmit` for memory injection — running both at once double-injects
memory context into every prompt.

## Configuration

Config is read from `~/.hindsight/coding-agent.json` (layered with any
nearer project-level config file). Key fields:

- `apiUrl` — Hindsight API base URL (default `http://localhost:8888`)
- `apiToken` — bearer token, if your server requires one
- `disabled` — hard off-switch; when `true`, silences all four surfaces
- `bankId` / `bankIdTemplate` — explicit static bank id, or the dynamic
  per-repo template (default `{gitProject}`)

Fact/memory extraction runs server-side and asynchronously — it's decoupled
from the coding agent's own model and token budget.

## Auto-seed flow

The first time you use the plugin in a new repo, `SessionStart` checks
whether the repo's bank already has git-sourced memory. If not, it offers to
seed it. On yes, a background backfill of recent git history starts
(non-blocking — extraction happens server-side/async, so your session isn't
held up). If you decline, that choice is remembered per-bank and you won't
be asked again for that repo.

## Migration from v1 (`hindsight-memory`)

v1 reads config from `~/.hindsight/claude-code.json`; v2 reads from
`~/.hindsight/coding-agent.json`. **These are a different file with a
different schema — your v1 config does not carry over automatically.**
Re-create whatever settings you need (API URL, token, bank id, etc.) in
`coding-agent.json`.

Both plugins can be installed side by side, but enable only one at a time —
see the install warning above.

## Troubleshooting

**It stopped offering to seed a repo.** Seed state is tracked per bank at
`~/.hindsight/coding-agent-state/<bankId>.json` (the bank id is
URL-encoded in the filename). Delete that file to be re-offered on the next
session, or force a seed right now:

```bash
node <plugin>/dist/hindsight-seed.js seed --repo .
```

Note that a bank marked as seeded or already warm won't be re-offered even
if it later goes cold again server-side.

**Is memory actually running?** Check `/tmp/hindsight-plugin.log` (override
with `HINDSIGHT_DIAG_FILE`) for `recall_ok` / `reflect_ok` / `retain_ok`
lines.

**Turn it off.** Set `"disabled": true` in `~/.hindsight/coding-agent.json`
— this silences all four surfaces.
