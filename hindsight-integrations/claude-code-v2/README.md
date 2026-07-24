# hindsight-memory-v2 (Claude Code plugin)

A thin Claude Code marketplace-style plugin wrapper around the shared
`hindsight-coding-agents` TypeScript core. It installs a single
`UserPromptSubmit` hook (`hooks/hooks.json`) that runs the bundled
`dist/claude-hook.js` on every prompt:

- **Every turn**: recalls relevant long-term memory and injects it as
  `additionalContext`.
- **First turn of a session**: also runs a one-time reflect pass, caching the
  outcome so later prompts in the same session only recall.
- Fails open: if no Hindsight server is reachable, or anything errors, the
  hook exits `0` and simply emits no context — it never breaks the agent
  turn.

This is a wrapper only. All behavior (recall/reflect logic, prompt
formatting, config resolution) lives in
`../hindsight-coding-agents/src/claude-hook.ts` and is bundled in via
`scripts/build.mjs`.

## Layout

```
.claude-plugin/plugin.json   # plugin manifest
hooks/hooks.json             # UserPromptSubmit -> node dist/claude-hook.js
scripts/build.mjs            # builds the core (tsup) and copies dist/claude-hook.js here
scripts/dev-install.sh       # manual, local-only install into ~/.claude (NOT run by build/CI)
dist/                        # build output (gitignored, reproducible via build.mjs)
```

## Build

```bash
node hindsight-integrations/claude-code-v2/scripts/build.mjs
```

This runs `npm run build` (tsup) in `../hindsight-coding-agents` and copies
the resulting `dist/claude-hook.js` into this plugin's `dist/`.

## Config

The hook reads its configuration from `~/.hindsight/coding-agent.json`
(merged with the nearest project-level config file), the same layered config
used by every harness in `hindsight-coding-agents`. Set the Hindsight API
base URL, bank, and any recall/reflect options there.

## Dev install (manual, local only)

```bash
bash hindsight-integrations/claude-code-v2/scripts/dev-install.sh
```

This builds the plugin and copies it into
`~/.claude/plugins/cache/hindsight/hindsight-memory-v2/0.1.0/`. It does
**not** touch `~/.claude/plugins/installed_plugins.json` or
`~/.claude/settings.json` — it prints the exact JSON snippets to add to each,
and reminds you to disable the existing `hindsight-memory` plugin first (to
avoid double-injecting memory context) and to restart Claude Code afterward.

Run this only when you intentionally want to change your live Claude Code
plugin setup.
