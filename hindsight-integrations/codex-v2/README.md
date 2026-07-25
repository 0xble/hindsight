# hindsight-codex-v2

Codex CLI wrapper for Hindsight memory, on the shared `hindsight-coding-agents` core — the Codex
sibling of `claude-code-v2`. Same core, same bank, thin Codex-specific packaging.

## What it does (v1)

- **SessionStart** → on a cold repo, deterministically starts a background seed (aggregated git-log
  + Haiku codebase survey) and injects the knowledge-page roster + tool guide.
- **UserPromptSubmit** → per-turn recall (≤750 tokens) injected with the attribution + user-feedback
  block, plus a periodic page-roster/tool-guide refresh.
- **MCP tools** (`hindsight_*`) → `list_knowledge_pages`, `read_knowledge_page`, `search_memory`,
  `capture_initiative`, `ingest_document`, wired via `~/.codex/config.toml [mcp_servers.hindsight]`
  with `HINDSIGHT_MCP_HARNESS=codex`.

**Not yet:** the `Stop` write-back (live sessions compounding into memory) — Codex's rollout JSONL
transcript differs from Claude's, so it needs its own transcript reader. Fast-follow.

## Install (dev)

```bash
bash scripts/dev-install.sh
```

Builds the bundle, writes `~/.codex/hooks.json` (backs up any existing one), and prints the two
`~/.codex/config.toml` snippets to add (`[features] codex_hooks = true` and
`[mcp_servers.hindsight]`). Restart Codex. Requires Codex CLI ≥ 0.116.

## Config

Shares `~/.hindsight/coding-agent.json` with every other harness (harness section: `harnesses.codex`).
Bank resolution mirrors Claude's, so both agents land in the same per-repo bank.
