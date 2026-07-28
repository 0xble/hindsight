
# Coding Agents

Long-term **project memory** for coding agents, from one package: a shared reflect-and-inject core
with a thin entry point per agent — **opencode**, **Claude Code**, **Codex CLI**, **Gemini CLI**, **Cursor CLI** —
Ingestion into a [Hindsight](https://vectorize.io/hindsight) memory bank is fully automatic — no
setup command: git history and conversations flow in as you work.

The premise: most of a real fix is derivable from the code, but the *last mile* often hinges on a
project-specific decision that isn't in the code at all — a rounding rule, a retry allowlist, a
tie-break policy. Those decisions live in git history and past conversations. This plugin puts them
in front of the agent at the moment it starts working.

## How it works

1. **Automatic ingestion (no setup).** A cold repo is seeded in the background the first time an
   agent opens it (aggregated commit-message history + a headless codebase survey), and every
   session start fires an idempotent background **deepen engine** that ingests recent commits
   individually with their full diffs — newest first, a bounded batch per session — plus any
   conversations not yet in the bank. Retain strategies are tuned per content type, knowledge pages
   are synthesized from the extracted facts, and every item carries a `REF-ID` tracer. The
   `hindsight_sync_status` tool reports where ingestion stands (`synced: true` = seeded memory
   fully queryable).
2. **Reflect once per session.** On the session's first task message, the entry point sends that
   message to Hindsight `reflect`, which reasons over the bank and returns a synthesized
   **root-cause answer** — the exact rule and literal values that were decided, with citations.
3. **Inject every turn.** The reflect answer is pushed into the agent's context (system prompt on
   opencode; hook context on the hook harnesses, cached per session and re-injected on later
   prompts) so it survives long sessions and correction rounds. Alongside it, the repo's
   **knowledge pages** are matched *locally* against each prompt (a lexical section index — no
   server round-trip, no LLM call) and the top-scoring page sections are injected with provenance
   and a pointer to the full page; below a relevance floor nothing is injected.
4. **Write back.** Each session is upserted into the bank as a JSON transcript — user/assistant
   turns plus a compact `action` turn per tool call (tool name + primary target, no arguments or
   outputs) — so sessions compound into memory. On Stop for the hook harnesses, every few turns for
   the opencode plugin. Cold repos are auto-seeded (aggregated git log + a short headless codebase
   survey) the first time an agent opens them.
5. **Never break the agent — never fail silently.** A failed reflect or page fetch degrades to
   no-memory, but every outcome (`reflect_ok` / `reflect_empty` / `reflect_failed`, `pages_ok` /
   `pages_failed`, with duration and error) is appended to a diagnostics file, so a memory-less
   session can't masquerade as a memory session.

When memories **conflict** on the same rule, reflect prefers the latest/superseding decision — a
rule amended in a later conversation wins over the original, and the superseded rule is reported as
no longer in effect.

## Supported agents

| harness       | kind              | entry point             | install |
| ------------- | ----------------- | ----------------------- | ------- |
| `opencode`    | persistent plugin | package default export  | add the package dir to `opencode.json` → `"plugin": [...]` |
| `claude-code` | per-prompt hook   | `hindsight-claude-hook` | `UserPromptSubmit` hook in Claude Code `settings.json` |
| `codex`       | per-prompt hook   | `hindsight-codex-hook`  | `UserPromptSubmit` hook in `~/.codex/hooks.json` (+ `codex_hooks = true`, Codex CLI ≥ 0.116) |
| `gemini`      | per-prompt hooks  | gemini wrapper hooks    | `SessionStart` + `BeforeAgent` + `SessionEnd` in `~/.gemini/settings.json` (Gemini CLI ≥ 0.52.0) |
| `cursor-cli`  | per-prompt hook   | `hindsight-cursor-hook` | `beforeSubmitPrompt` hook in Cursor `hooks.json` |

```json title="opencode.json"
{ "plugin": ["/path/to/hindsight-coding-agents"] }
```

```json title="Claude Code settings.json — Codex ~/.codex/hooks.json is identical (command: hindsight-codex-hook)"
{ "hooks": { "UserPromptSubmit": [ { "hooks": [
    { "type": "command", "command": "hindsight-claude-hook" } ] } ] } }
```

```json title="Cursor hooks.json"
{ "hooks": { "beforeSubmitPrompt": [ { "command": "hindsight-cursor-hook" } ] } }
```

The opencode plugin additionally exposes an on-demand **`memory_reflect` tool** (same synthesized
reflect, callable mid-task) and opt-in **incremental git-sync** and **live session write-back**
(see the configuration reference).

## Configuration

All configuration is **one JSON file**: `~/.hindsight/coding-agent.json` (no environment
variables; exception: `HINDSIGHT_DIAG_FILE`). Later wins per field: built-in defaults → the file's
top level → its `harnesses.<name>` section for the asking agent. There is no repo-carried config —
per-repo bank routing is `directoryBankMap`, per-agent differences are `harnesses.<name>`.

Each entry point knows which harness it *is*, so one shared config serves several agents side by
side:

```jsonc
{
  "apiUrl": "http://localhost:8888",
  "harnesses": {
    "opencode":    { "reflectTimeoutMs": 60000 },
    "claude-code": { "disabled": true }          // e.g. memory off for Claude only
  }
}
```

### Reference

| field | default | meaning |
| --- | --- | --- |
| `apiUrl` | `http://localhost:8888` | Hindsight API base URL |
| `apiToken` | — | bearer token (Hindsight Cloud) |
| `bankId` | — | **explicit static bank**; unset ⇒ per-repo dynamic resolution (below) |
| `dynamicBankId` | dynamic iff no `bankId` | force dynamic (`true`) or static (`false`) resolution |
| `bankIdTemplate` | `"{gitProject}"` | dynamic bank id format, e.g. `"hindsight-{gitProject}"` |
| `directoryBankMap` | — | absolute path → bank; **longest prefix wins**; overrides everything |
| `resolveWorktrees` | `true` | `{gitProject}`: linked worktrees share the main repo's bank |
| `disabled` | `false` | hard off-switch (inert plugin/hook — a no-memory baseline) |
| `reflectTimeoutMs` | `120000` | session-reflect timeout (hook harnesses cap it at 25s to fit the host's hook window); on timeout the session runs without reflect (recorded in diagnostics) |
| `pageRefreshEveryTurns` | `10` | refetch the knowledge pages and re-inject the page roster + tool guide every N user turns |
| `retainSessions` | `true` | opencode write-back: async upsert every turn (set `false` to opt out; hook harnesses always write on Stop) |
| `retainEveryTurns` | `1` | write-back cadence (user turns) |
| `gitIngest` | `"message"` | git depth for seeding and staying current: `"message"` (messages only), `"full"` (messages + per-commit diffs), `"none"` |
| `harnesses.<name>` | — | per-harness override of any field above |

### Bank resolution — per-repo by default

Coding memory is **per repository**. Resolution order for the working directory:

1. `directoryBankMap` — longest matching absolute-path prefix (mapping a repo root covers every
   subdirectory; deeper mappings win; overrides even an explicit `bankId`).
2. Static — `bankId` set (or `dynamicBankId: false`).
3. Dynamic — `bankIdTemplate` with placeholders:
   - `{gitProject}` — worktree-aware repo name: every linked worktree resolves to the **main**
     worktree's basename, so all worktrees of a repo share one bank (bare repos use the bare dir
     name; non-git directories fall back to the dir basename)
   - `{project}` — plain working-directory basename
   - `{harness}` — the entry point asking (`opencode`, `claude-code`, `codex`, `cursor-cli`)
   - `{channel}` / `{user}` — `$HINDSIGHT_CHANNEL_ID` / `$HINDSIGHT_USER_ID`

The default `"{gitProject}"` means **all agents share one memory per repo** — use
`"{harness}-{gitProject}"` to split per agent instead.

## Diagnostics

Every reflect and page-fetch outcome is appended as a JSON line to `/tmp/hindsight-plugin.log` (override with
`HINDSIGHT_DIAG_FILE`):

```json
{"ts":"2026-07-23T07:05:52Z","harness":"claude-code","event":"reflect_ok","ms":15816,"chars":324,"query":"..."}
```

`reflect_failed` records the error; if you're comparing memory-on vs memory-off, check this file —
a run whose reflects failed is a no-memory run.
