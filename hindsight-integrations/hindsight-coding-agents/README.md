# hindsight-coding-agents

Long-term project memory for **coding agents**, backed by [Hindsight](https://vectorize.io/hindsight).
One package, several agents: a shared reflect-and-inject core with a thin entry point per agent
(**opencode**, **Claude Code**, **Codex CLI**, **Gemini CLI**, **Cursor CLI**). Ingestion is fully
automatic — there is no setup command: a repo's git history and conversations flow into its memory
bank in the background as you work.

The premise: most of a real fix is derivable from the code, but the _last mile_ often hinges on a
project-specific decision that isn't in the code at all — a rounding rule, a retry allowlist, a
tie-break policy. Those decisions live in git history and past conversations. This package puts them
in front of the agent at the moment it starts working, and keeps a curated set of **knowledge pages**
(architecture, conventions, in-flight initiatives) that future sessions start from.

## How it works

1. **Seed a cold repo (automatic, once).** The first time an agent opens a repo whose bank is empty,
   the entry point deterministically kicks off a background **seed**: it aggregates the last N commit
   **messages** into a single cheap document (`gitlog` strategy) and spawns a short headless
   **codebase survey** — run under the current agent's own CLI (claude/codex/gemini/opencode),
   read-only sandboxed — to map the structure. Both feed the knowledge pages.
1. **Deepen progressively (automatic, every session).** Every session start also fires the
   idempotent background **deepen engine**: it ingests any conversations not yet in the bank and the
   next batch of recent commits **individually with their full diffs, newest first** — so full
   decision-level precision arrives across sessions without a big-bang ingest. A per-bank lock and
   document-id dedup make concurrent or repeated runs a no-op. Ask the agent
   `hindsight_sync_status` to see where ingestion stands (`synced: true` = seeded memory fully
   queryable).
2. **Reflect once per session.** On the session's first prompt, the entry point runs Hindsight
   `reflect` — an agentic synthesis over the bank that returns the past decision explaining the task
   at hand, with its exact rule and literal values. The answer is cached for the session and
   re-injected on every turn, wrapped in a visible-attribution directive so the agent surfaces a
   `🧠 Using Hindsight Memories` header when memory informs its answer.
3. **Knowledge-page sections every turn.** The repo's knowledge pages are fetched on a cadence and
   matched **locally** against each prompt (a lexical section index — no server round-trip, no LLM
   call, ~ms): the top-scoring page sections are injected with provenance and a pointer to the full
   page. Fast like recall, organized like reflect; below a relevance floor nothing is injected.
4. **Knowledge pages + tools.** At session start the agent is given the repo's page roster plus a
   guide to the `hindsight_*` tools; it lists/reads pages to ground itself, searches raw memory for
   specifics (`hindsight_search_memory` is where recall lives now), and calls
   `hindsight_capture_initiative` right after a plan is approved to record a new feature as a tracked
   page. On opencode these tools are registered natively; the hook harnesses get them through the
   bundled **MCP server**.
5. **Write back.** The live session is upserted into the bank as a JSON transcript — user/assistant
   turns plus a compact `action` turn per tool call (tool name + primary target, e.g.
   `Edit boltons/strutils.py`; no arguments or outputs) — so sessions compound into memory without
   burying decisions in mechanical noise. On Stop for the hook harnesses, every few turns for the
   opencode plugin.
6. **Never break the agent — never fail silently.** A failed reflect or page fetch degrades to
   no-memory, but every outcome (`reflect_ok` / `reflect_empty` / `reflect_failed`, `pages_ok` /
   `pages_failed`, with duration and error) is appended to a diagnostics file, so a memory-less
   session can't masquerade as a memory session.

When memories **conflict** on the same rule, reflect prefers the latest/superseding decision — a rule
amended in a later conversation wins over the original.

## Harnesses

Every harness runs the same surface (seed → session reflect → per-turn page sections → knowledge
tools → write-back); they differ only in how that surface is delivered.

| harness       | kind              | lifecycle wiring                                                                                                       | install                                                              |
| ------------- | ----------------- | ---------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------- |
| `opencode`    | persistent plugin | one process: load-time seed, session reflect + page sections, native tools, write-back                                                 | add the package dir to `opencode.json` → `"plugin": [...]`           |
| `claude-code` | per-prompt hooks  | `SessionStart` (seed) + `UserPromptSubmit` (reflect + pages) + `Stop` (write-back) + MCP                                        | the [`../claude-code-v2`](../claude-code-v2) wrapper's dev-installer |
| `codex`       | per-prompt hooks  | same three hooks in `~/.codex/hooks.json` (+ `codex_hooks = true`, CLI ≥ 0.116)                                        | the [`../codex-v2`](../codex-v2) wrapper's dev-installer             |
| `gemini`      | per-prompt hooks  | `SessionStart` + `BeforeAgent` (reflect + pages) + `SessionEnd` (write-back) + MCP, in `~/.gemini/settings.json` (CLI ≥ 0.52.0) | the [`../gemini-v2`](../gemini-v2) wrapper's dev-installer           |
| `cursor-cli`  | per-prompt hook   | `beforeSubmitPrompt` (reflect + pages only — Cursor lacks a usable session/stop hook)                                           | `beforeSubmitPrompt` hook in Cursor `hooks.json`                     |

The hook-based harnesses share one runtime (`src/core/hook.ts`) plus their SessionStart/Stop
entrypoints; opencode is the cleanest platform — a real per-turn event, a working system-prompt
injection channel, transcript access, and native tool registration — so the whole surface rides four
plugin hooks (`src/harness/opencode.ts`) with **no MCP server needed**. It also supports opt-in
**incremental git-sync** (retain commits new since the seed on load).

### Install — one command, every agent

```bash
npx hindsight-coding-agents install            # detects your agents, wires each natively
npx hindsight-coding-agents install codex      # or pick specific harnesses
npx hindsight-coding-agents uninstall          # removes exactly what install added
```

`install` merges the native wiring (hooks + MCP registration where the host wants them) into each
agent's own config, preserving everything already there; it is idempotent (re-run after moving the
package) and backs up any pre-existing file it touches as `<file>.hindsight-backup`. `uninstall`
removes only our entries. Manual wiring per harness, if you prefer:

**opencode** installs directly — point `opencode.json` at the package dir:

```json
{ "plugin": ["/path/to/hindsight-coding-agents"] }
```

**Claude Code** and **Codex** get the full three-hook + MCP wiring from their sibling wrapper
packages ([`../claude-code-v2`](../claude-code-v2), [`../codex-v2`](../codex-v2)) — thin bundles of
this core whose `scripts/dev-install.sh` writes the settings/hooks pointing at each bundled
`dist/*.js`. This package's `bin` entries (`hindsight-claude-hook`, `hindsight-codex-hook`,
`hindsight-cursor-hook`) are the individual injection-only `UserPromptSubmit` entrypoints for a
minimal, hand-wired setup.

Adding an agent: hook-based → write a `HookSpec` entry point (see `src/cursor-hook.ts`) and register
a `hookAdapter` in `src/harness/registry.ts`; persistent-plugin → implement `HarnessAdapter`
(`src/core/types.ts`) fully (see `src/harness/opencode.ts`).

## Configuration

All configuration is **ONE JSON file, no environment variables** (exception: `HINDSIGHT_DIAG_FILE`
for the diagnostics path): `~/.hindsight/coding-agent.json`. Layering, later wins per field:

1. built-in defaults
2. the file's top level
3. its `harnesses.<name>` section — per-agent override

There is deliberately no repo-carried config file — per-repo bank routing is `directoryBankMap`,
per-agent differences are `harnesses.<name>`.

Each entry point knows which harness it _is_ (the opencode plugin is loaded by opencode, the codex
hook by Codex...), so one shared config serves several agents side by side:

```jsonc
{
  "apiUrl": "https://api.hindsight.vectorize.io",
  "harnesses": {
    "opencode": { "reflectTimeoutMs": 60000 },
    "claude-code": { "disabled": true }, // e.g. memory off for Claude only
  },
}
```

### Reference

| field                   | default                              | meaning                                                                                                                                                               |
| ----------------------- | ------------------------------------ | --------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `apiUrl`                | `https://api.hindsight.vectorize.io` | Hindsight API base URL (set to `http://localhost:8888` for a local server)                                                                                            |
| `apiToken`              | —                                    | bearer token (Hindsight Cloud)                                                                                                                                        |
| `bankId`                | —                                    | **explicit static bank**; unset ⇒ per-repo dynamic resolution (below)                                                                                                 |
| `dynamicBankId`         | dynamic iff no `bankId`              | force dynamic (`true`) or static (`false`) resolution                                                                                                                 |
| `bankIdTemplate`        | `"coding-agent::{gitProject}"`       | dynamic bank id format; the default makes every agent share one bank per repo                                                                                         |
| `directoryBankMap`      | —                                    | absolute path → bank; **longest prefix wins**; overrides everything                                                                                                   |
| `resolveWorktrees`      | `true`                               | `{gitProject}`: linked worktrees share the main repo's bank                                                                                                           |
| `disabled`              | `false`                              | hard off-switch (inert plugin/hook — a no-memory baseline)                                                                                                            |
| `reflectTimeoutMs`      | `120000`                             | session-reflect timeout (hook harnesses additionally cap it at 25s to fit the host's hook window); on timeout the session runs without reflect (recorded)             |
| `pageRefreshEveryTurns` | `10`                                 | refetch the knowledge pages and re-inject the page roster + tool guide every N user turns                                                                             |
| `autoSeed`              | `true`                               | SessionStart: auto-seed a cold repo's bank from git history                                                                                                           |
| `seedLimit`             | `300`                                | auto-seed: most-recent-N-commits cap                                                                                                                                  |
| `codebaseSurvey`        | `true`                               | SessionStart: headless survey of a cold repo's structure, run under the current harness's own CLI (claude/codex/gemini/opencode), falling back to any available agent |
| `surveyModel`           | `haiku`                              | model for the survey — Claude recipe only (`claude -p --model`); other agents use their configured default                                                            |
| `surveyBudgetUsd`       | `2`                                  | survey spend cap — Claude recipe only (`claude -p --max-budget-usd`); other agents rely on their read-only sandbox                                                    |
| `retainSessions`        | `true`                               | opencode write-back: async upsert of the session transcript every turn (set `false` to opt out; hook harnesses always write on Stop)                                  |
| `retainEveryTurns`      | `1`                                  | opencode write-back cadence (user turns)                                                                                                                              |
| `logLevel`              | `"info"`                             | plugin-log verbosity (`"debug"` \| `"info"` \| `"warn"` \| `"error"`); `HINDSIGHT_LOG_LEVEL` env overrides                                                             |
| `gitIngest`             | `"message"`                             | git depth for seeding AND staying current (same engine): `"message"` = commit messages only (one doc, re-upserted when HEAD moves); `"full"` = messages + per-commit full diffs (progressive, newest first); `"none"` = git off |
| `harnesses.<name>`      | —                                    | per-harness override of any field above                                                                                                                               |
| `harness`               | `opencode`                           | **deepen engine only**: which session format `--conversations` is read as                                                                                                  |

### Bank resolution

Coding memory is **per repository**. Resolution order for the working directory:

1. `directoryBankMap` — longest matching absolute-path prefix (mapping a repo root covers every
   subdirectory; deeper mappings win; overrides even an explicit `bankId`).
2. Static — `bankId` set (or `dynamicBankId: false`).
3. Dynamic — `bankIdTemplate` with placeholders:
   - `{gitProject}` — worktree-aware repo name: `git rev-parse --git-common-dir` resolves every
     linked worktree to the **main** worktree's basename, so all worktrees of a repo share one bank
     (bare repos use the bare dir name; non-git directories fall back to the dir basename)
   - `{project}` — plain working-directory basename
   - `{harness}` — the entry point asking (`opencode`, `claude-code`, `codex`, `gemini`, `cursor-cli`)
   - `{channel}` / `{user}` — `$HINDSIGHT_CHANNEL_ID` / `$HINDSIGHT_USER_ID`

The default `"coding-agent::{gitProject}"` is **harness-neutral**, so opencode, Claude Code, and Codex
all share one memory per repo — use `"{harness}-{gitProject}"` to split per agent instead.

## Ingestion internals (no CLI)

There is no user-facing ingest command — the deepen engine (`dist/deepen.js`) is spawned by every
session start and does only the missing work: bank configuration, conversation import (dedup by
document id), the one-time gitlog seed, the next per-commit diff batch (newest first, bounded per
run), then knowledge pages once extraction has drained. Harnesses that need deterministic ingestion
(benchmarks, e2e suites) run the same engine directly and poll `dist/status.js` until
`"synced": true` — the exact readiness contract the `hindsight_sync_status` agent tool reports.

Past-conversation import accepts a normalized interchange file (engine `--conversations` flag):
`[{ "id": "s1", "turns": [{ "role": "user", "text": "...", "timestamp?": "ISO" }, ...] }, ...]`,
chronological (a later chat can amend an earlier one). Day-to-day, conversations simply accrue from
the live session write-back — no export step.

Local Hindsight for trying it out:

```bash
docker run -d -p 8888:8888 -p 9999:9999 -e HINDSIGHT_API_LLM_PROVIDER=gemini \
  -e HINDSIGHT_API_LLM_API_KEY=$GEMINI_API_KEY -e HINDSIGHT_API_LLM_MODEL=gemini-2.5-flash \
  ghcr.io/vectorize-io/hindsight:latest
```

## Diagnostics & logging

Two files, two audiences:

**Leveled plugin log** (humans debugging): `$TMPDIR/hindsight-coding-agent/plugin.log` (override
`HINDSIGHT_LOG_FILE`) — timestamped `LEVEL [scope] message` lines from every component, including
the ingestion engine. Level defaults to `info`; set `"logLevel": "debug"` in config or
`HINDSIGHT_LOG_LEVEL=debug` for ad-hoc debugging (at `debug`, every diag event below is mirrored
here too, so one file tells the whole story).

**Structured diag events** (machines/harnesses): every reflect and page-fetch outcome is appended
as a JSON line to `/tmp/hindsight-plugin.log` (override with `HINDSIGHT_DIAG_FILE`):

```json
{
  "ts": "2026-07-27T07:05:52Z",
  "harness": "claude-code",
  "event": "reflect_ok",
  "ms": 14210,
  "chars": 792,
  "query": "..."
}
```

`reflect_failed` / `pages_failed` record the error; if you're comparing memory-on vs memory-off,
check this file — a run whose reflects failed is a no-memory run. Seed starts are logged as
`seed_started`.

## Testing

```bash
npm test          # unit tests: bank resolution, config layering, transcript readers, hook/reflect logic, page-section index (no network)
npm run test:live # LIVE system test against a real server + real LLM:
                  #   HINDSIGHT_API_URL=http://localhost:8888 npm run test:live
```

The live suite builds a real git repo with a decision planted in a commit and a conversation, runs
the real backfill (server-side LLM extraction), then drives the built hook binaries as subprocesses
and asserts the decision's literal values come back in the injected context.

## Layout

```
src/
  core/          # harness-agnostic: config (layered), bank resolution, hindsight client, missions,
                 # git + chat ingest, git-sync, seed + survey, pages-index (local section matching),
                 # knowledge-injection, knowledge-tools, session-start, transcript readers,
                 # hook runtime, RuntimeCore
  harness/       # per-agent adapters + registry (opencode persistent; claude/codex/gemini/cursor as hooks)
  index.ts       # opencode plugin entrypoint
  claude-hook.ts / claude-sessionstart-hook.ts / claude-stop-hook.ts   # Claude Code entrypoints
  codex-hook.ts  / codex-sessionstart-hook.ts  / codex-stop-hook.ts    # Codex CLI entrypoints
  gemini-hook.ts / gemini-sessionstart-hook.ts / gemini-stop-hook.ts   # Gemini CLI entrypoints
  cursor-hook.ts # Cursor CLI hook entrypoint   (bin: hindsight-cursor-hook)
  mcp-server.ts  # hindsight_* knowledge/recall tools for the hook harnesses (MCP)
  backfill.ts    # backfill CLI                  (bin: hindsight-coding-backfill)
```
