# hindsight-gemini-v2

Gemini CLI wrapper for Hindsight memory, on the shared `hindsight-coding-agents` core — the Gemini
sibling of `claude-code-v2` and `codex-v2`. Same core, same bank, thin Gemini-specific packaging.

## What it does

- **SessionStart** → on a cold repo, deterministically starts a background seed (aggregated git-log
  + Haiku codebase survey) and injects the knowledge-page roster + tool guide.
- **BeforeAgent** (Gemini's per-turn event) → per-turn recall (≤750 tokens) injected via
  `hookSpecificOutput.additionalContext`, with the attribution + user-feedback block, plus a periodic
  page-roster/tool-guide refresh.
- **SessionEnd** → write-back: the session's JSONL transcript is parsed (`transcript-gemini.ts`) and
  retained under the verbose `session` strategy, so Gemini sessions compound into memory.
- **MCP tools** (`hindsight_*`) → `list_knowledge_pages`, `read_knowledge_page`, `search_memory`,
  `capture_initiative`, `ingest_document`, wired via `~/.gemini/settings.json` `mcpServers.hindsight`
  with `HINDSIGHT_MCP_HARNESS=gemini`.

Full parity with `claude-code-v2` / `codex-v2`. Gemini CLI (≥ 0.52.0) added a Claude-Code-style hooks
system (stdin/stdout JSON), so the only Gemini-specific piece is the JSONL transcript reader for the
SessionEnd write-back. Gemini names the events differently — `BeforeAgent` (not `UserPromptSubmit`)
and `SessionEnd` (not `Stop`) — but the contract is the same.

## Install (dev)

```bash
bash scripts/dev-install.sh
```

Builds the bundle and **merges** the three hooks + the `mcpServers.hindsight` entry into
`~/.gemini/settings.json` (backing up the existing file first — Gemini keeps hooks and MCP in the same
settings file as your auth, so it merges rather than overwrites). Restart Gemini CLI.

Requires Gemini CLI ≥ 0.52.0 (older versions have no hooks system — check with `gemini --version`;
upgrade with `npm i -g @google/gemini-cli@latest`).

## Config

Shares `~/.hindsight/coding-agent.json` with every other harness (harness section: `harnesses.gemini`).
Bank resolution mirrors Claude's, so all agents land in the same per-repo bank
(`coding-agent::{gitProject}`).
