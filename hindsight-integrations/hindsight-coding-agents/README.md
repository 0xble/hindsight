# hindsight-coding-agents

Long-term project memory for **coding agents**, backed by [Hindsight](https://vectorize.io/hindsight).
One package, several agents: a shared recall-and-inject core with a thin entry point per agent
(**opencode**, **Claude Code**, **Codex CLI**, **Gemini CLI**, **Cursor CLI**), plus a one-shot **backfill** CLI that
ingests a repo's git history and past developer conversations into a memory bank.

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
   read-only sandboxed — to map the structure. Both feed the knowledge pages. You can also
   run the seed explicitly with the `backfill` CLI (below), including `--diffs` for verbose
   per-commit decision extraction and `--conversations` to ingest past developer chats.
2. **Recall every turn.** On each user prompt, the entry point calls Hindsight `recall` with the
   prompt and injects a compact `<hindsight_memories>` block (default ≤750 tokens) — the relevant
   facts with their `REF-ID` citations, wrapped in a visible-attribution directive so the agent
   surfaces a `🧠 Using Hindsight Memories` header when memory informs its answer.
3. **Knowledge pages + tools.** At session start the agent is given the repo's page roster plus a
   guide to the `hindsight_*` tools; it lists/reads pages to ground itself, searches raw memory for
   specifics, and calls `hindsight_capture_initiative` right after a plan is approved to record a new
   feature as a tracked page. On opencode these tools are registered natively; the hook harnesses get
   them through the bundled **MCP server**.
4. **Write back.** The live session is upserted into the bank as a rich transcript (text + tool calls
   and their output) so sessions compound into memory — on Stop for the hook harnesses, every few
   turns for the opencode plugin.
5. **Never break the agent — never fail silently.** A failed recall degrades to no-memory, but every
   outcome (`recall_ok` / `recall_empty` / `recall_failed`, with duration and error) is appended to a
   diagnostics file, so a memory-less session can't masquerade as a memory session.

When memories **conflict** on the same rule, recall prefers the latest/superseding decision — a rule
amended in a later conversation wins over the original.

## Harnesses

Every harness runs the same v2 surface (seed → per-turn recall → knowledge tools → write-back); they
differ only in how that surface is delivered.

| harness       | kind              | lifecycle wiring                                                                                                       | install                                                              |
| ------------- | ----------------- | ---------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------- |
| `opencode`    | persistent plugin | one process: load-time seed, per-turn recall, native tools, write-back                                                 | add the package dir to `opencode.json` → `"plugin": [...]`           |
| `claude-code` | per-prompt hooks  | `SessionStart` (seed) + `UserPromptSubmit` (recall) + `Stop` (write-back) + MCP                                        | the [`../claude-code-v2`](../claude-code-v2) wrapper's dev-installer |
| `codex`       | per-prompt hooks  | same three hooks in `~/.codex/hooks.json` (+ `codex_hooks = true`, CLI ≥ 0.116)                                        | the [`../codex-v2`](../codex-v2) wrapper's dev-installer             |
| `gemini`      | per-prompt hooks  | `SessionStart` + `BeforeAgent` (recall) + `SessionEnd` (write-back) + MCP, in `~/.gemini/settings.json` (CLI ≥ 0.52.0) | the [`../gemini-v2`](../gemini-v2) wrapper's dev-installer           |
| `cursor-cli`  | per-prompt hook   | `beforeSubmitPrompt` (recall only — Cursor lacks a usable session/stop hook)                                           | `beforeSubmitPrompt` hook in Cursor `hooks.json`                     |

The hook-based harnesses share one runtime (`src/core/hook.ts`) plus their SessionStart/Stop
entrypoints; opencode is the cleanest platform — a real per-turn event, a working system-prompt
injection channel, transcript access, and native tool registration — so the whole surface rides four
plugin hooks (`src/harness/opencode.ts`) with **no MCP server needed**. It also supports opt-in
**incremental git-sync** (retain commits new since the seed on load).

**opencode** installs directly — point `opencode.json` at the package dir:

```json
{ "plugin": ["/path/to/hindsight-coding-agents"] }
```

**Claude Code** and **Codex** get the full three-hook + MCP wiring from their sibling wrapper
packages ([`../claude-code-v2`](../claude-code-v2), [`../codex-v2`](../codex-v2)) — thin bundles of
this core whose `scripts/dev-install.sh` writes the settings/hooks pointing at each bundled
`dist/*.js`. This package's `bin` entries (`hindsight-claude-hook`, `hindsight-codex-hook`,
`hindsight-cursor-hook`) are the individual **recall-only** `UserPromptSubmit` entrypoints for a
minimal, hand-wired setup.

Adding an agent: hook-based → write a `HookSpec` entry point (see `src/cursor-hook.ts`) and register
a `hookAdapter` in `src/harness/registry.ts`; persistent-plugin → implement `HarnessAdapter`
(`src/core/types.ts`) fully (see `src/harness/opencode.ts`).

## Configuration

All configuration is **JSON files, no environment variables** (exception: `HINDSIGHT_DIAG_FILE` for
the diagnostics path). Layering, later wins per field:

1. built-in defaults
2. `~/.hindsight/coding-agent.json` — user-global
3. its `harnesses.<name>` section — per-agent override
4. the **nearest** `<dir>/.hindsight/coding-agent.json` at or above the working directory —
   project-local (the natural home for per-repo settings)
5. its `harnesses.<name>` section

Each entry point knows which harness it _is_ (the opencode plugin is loaded by opencode, the codex
hook by Codex...), so one shared config serves several agents side by side:

```jsonc
{
  "apiUrl": "https://api.hindsight.vectorize.io",
  "harnesses": {
    "opencode": { "recallMaxTokens": 1000 },
    "claude-code": { "disabled": true }, // e.g. memory off for Claude only
  },
}
```

### Reference

| field                   | default                              | meaning                                                                           |
| ----------------------- | ------------------------------------ | --------------------------------------------------------------------------------- |
| `apiUrl`                | `https://api.hindsight.vectorize.io` | Hindsight API base URL (set to `http://localhost:8888` for a local server)        |
| `apiToken`              | —                                    | bearer token (Hindsight Cloud)                                                    |
| `bankId`                | —                                    | **explicit static bank**; unset ⇒ per-repo dynamic resolution (below)             |
| `dynamicBankId`         | dynamic iff no `bankId`              | force dynamic (`true`) or static (`false`) resolution                             |
| `bankIdTemplate`        | `"coding-agent::{gitProject}"`       | dynamic bank id format; the default makes every agent share one bank per repo     |
| `directoryBankMap`      | —                                    | absolute path → bank; **longest prefix wins**; overrides everything               |
| `resolveWorktrees`      | `true`                               | `{gitProject}`: linked worktrees share the main repo's bank                       |
| `disabled`              | `false`                              | hard off-switch (inert plugin/hook — a no-memory baseline)                        |
| `recallMaxTokens`       | `750`                                | per-turn recall token budget                                                      |
| `recallTimeoutMs`       | `10000`                              | per-turn recall timeout; on timeout the turn runs without memory (recorded)       |
| `pageRefreshEveryTurns` | `10`                                 | re-inject the knowledge-page roster + tool guide every N user turns               |
| `autoSeed`              | `true`                               | SessionStart: auto-seed a cold repo's bank from git history                       |
| `seedLimit`             | `300`                                | auto-seed: most-recent-N-commits cap                                              |
| `codebaseSurvey`        | `true`                               | SessionStart: headless survey of a cold repo's structure, run under the current harness's own CLI (claude/codex/gemini/opencode), falling back to any available agent |
| `surveyModel`           | `haiku`                              | model for the survey — Claude recipe only (`claude -p --model`); other agents use their configured default |
| `surveyBudgetUsd`       | `2`                                  | survey spend cap — Claude recipe only (`claude -p --max-budget-usd`); other agents rely on their read-only sandbox |
| `retainSessions`        | `true`                               | opencode write-back (set `false` to opt out; hook harnesses always write on Stop) |
| `retainEveryTurns`      | `5`                                  | opencode write-back cadence (user turns)                                          |
| `gitSync.enabled`       | `false`                              | opencode only: on load, retain commits new since the seed                         |
| `gitSync.ref`           | `origin/main`                        | git-sync target ref (falls back to `HEAD`)                                        |
| `gitSync.fetch`         | `false`                              | `git fetch` the ref before diffing                                                |
| `harnesses.<name>`      | —                                    | per-harness override of any field above                                           |
| `harness`               | `opencode`                           | **backfill only**: which session format `--conversations` is read as              |

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

## Backfill

The auto-seed covers the common case; the CLI is for explicit / richer ingests (full diffs, past
conversations, resets):

```bash
hindsight-coding-backfill --repo /path/to/repo \
  [--bank myproject] [--harness opencode] [--conversations sessions.json] \
  [--api-url http://localhost:8888] [--api-token X] [--config <path>] \
  [--diffs] [--limit 100] [--reset] [--no-pages] [--concurrency 8]
```

- Without `--bank`, the **same per-repo resolution** the runtime uses is applied to `--repo`, so
  `hindsight-coding-backfill --repo .` fills exactly the bank the agents will read.
- Default is the cheap `gitlog` aggregate; `--diffs` switches to the verbose `git` strategy (every
  commit's full message + diff — much more tokens; opt-in).
- `sessions.json` is the normalized interchange format any exporter can emit:
  `[{ "id": "s1", "turns": [{ "role": "user", "text": "...", "timestamp?": "ISO" }, ...] }, ...]`.
  Session list order is **chronological** — a later chat can amend an earlier one (last = newest).
- Chats are ingested **before** the git flood so decisions aren't starved in the extraction queue;
  the CLI drains extraction and reports `done/failed` counts before exiting.
- Tip: validate a setup with `--limit 100` before a full-history ingest.

Local Hindsight for trying it out:

```bash
docker run -d -p 8888:8888 -p 9999:9999 -e HINDSIGHT_API_LLM_PROVIDER=gemini \
  -e HINDSIGHT_API_LLM_API_KEY=$GEMINI_API_KEY -e HINDSIGHT_API_LLM_MODEL=gemini-2.5-flash \
  ghcr.io/vectorize-io/hindsight:latest
```

## Diagnostics

Every recall outcome is appended as a JSON line to `/tmp/hindsight-plugin.log` (override with
`HINDSIGHT_DIAG_FILE`):

```json
{
  "ts": "2026-07-25T07:05:52Z",
  "harness": "claude-code",
  "event": "recall_ok",
  "ms": 812,
  "count": 6,
  "query": "..."
}
```

`recall_failed` records the error; if you're comparing memory-on vs memory-off, check this file —
a run whose recalls failed is a no-memory run. Seed starts are logged as `seed_started`.

## Testing

```bash
npm test          # unit tests: bank resolution, config layering, transcript readers, hook/recall logic (no network)
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
                 # git + chat ingest, git-sync, seed + survey, recall, knowledge-injection,
                 # knowledge-tools, session-start, transcript readers, hook runtime, RuntimeCore
  harness/       # per-agent adapters + registry (opencode persistent; claude/codex/gemini/cursor as hooks)
  index.ts       # opencode plugin entrypoint
  claude-hook.ts / claude-sessionstart-hook.ts / claude-stop-hook.ts   # Claude Code entrypoints
  codex-hook.ts  / codex-sessionstart-hook.ts  / codex-stop-hook.ts    # Codex CLI entrypoints
  gemini-hook.ts / gemini-sessionstart-hook.ts / gemini-stop-hook.ts   # Gemini CLI entrypoints
  cursor-hook.ts # Cursor CLI hook entrypoint   (bin: hindsight-cursor-hook)
  mcp-server.ts  # hindsight_* knowledge/recall tools for the hook harnesses (MCP)
  backfill.ts    # backfill CLI                  (bin: hindsight-coding-backfill)
```
