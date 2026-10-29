# Coding Hook Prompts

Part of the root [maintenance contract](../MAINTENANCE.md).

## Intentional Divergence

At fork base `aa7d9724f7d6076f87d29163fd2baff1c0213595`, the coding-agents transcript reader already stripped harness transport wrappers, but automatic memory injection queried their raw contents. A synthetic first prompt could consume a session's initial injection or retry. The shared hook runtime now uses that existing tag rule for automatic queries and leaves wholly synthetic prompts outside session accounting. Explicit MCP reflection is unchanged.

Upstream guidance was inspected at `a9eeed77f9bce99d8f97d39a30d3281747300824` on `2026-10-11`, including AGENTS.md, CLAUDE.md and CONTRIBUTING.md. The independent approach was to reuse the existing structural filter before automatic query and cache work. Related [issue #3023](https://github.com/vectorize-io/hindsight/issues/3023) and merged [PR #3378](https://github.com/vectorize-io/hindsight/pull/3378) establish the same rule for retained transcripts. Merged [PR #4712](https://github.com/vectorize-io/hindsight/pull/4712) already supplies bounded retry after failed injection, so this patch preserves that implementation. Searches for synthetic hook prompts and task notifications found no automatic-query equivalent. Upstream accepts external bug reports rather than external PRs.

The previous reflection warning declared a session memory-less before retrieval fallback ran. The warning now records only the failed reflection. Existing fallback diagnostic events and the turn's notice continue to report the eventual outcome, without adding prompt or answer fields.

Known opening tag names must end with whitespace or `>`. Hyphen and colon suffixes remain literal text even if followed by a known closing tag, and unclosed wrappers are conservatively preserved. The automatic query and retained journal use this same exact-name rule.

The shared startup boundary adopts the document-presence correction from upstream [#5510](https://github.com/vectorize-io/hindsight/pull/5510) (`87b3e311d3e8b62e12660703988f75310e59a3ae`). An empty Knowledge Pages roster or missing git history does not prove the bank is empty. Defer automatic injection only after a successful one-document probe confirms emptiness before seeding. Absent, malformed or failed probes leave retrieval eligible. This changes no bank content, credentials, host wiring, model route or API runtime.

## Update And Verify

The maintained boundary is `hindsight-integrations/coding-agents/src/core/hook.ts`, shared by hook and persistent-plugin callers. `src/core/runtime.ts` excludes synthetic prompts before the plugin host's separate first-turn roster counter advances. `src/core/transcript-util.ts` owns the tag rule. `src/core/hook-synthetic.test.ts` drives the public hook runtime and native Claude lifecycle declaration, proving no network work or session mutation on synthetic prompts, preservation of real surrounding text and unknown suffixes, deferred new-bank prompts, later retry and truthful fallback logging. `src/core/turn-journal.test.ts` proves retained journal output preserves literal suffixes and excludes transport-only turns. `src/core/runtime.test.ts` proves that a plugin's first real prompt receives its live page roster after synthetic prompts. The package's existing `npm test`, `npm run build` and TypeScript check cover the integration. The exact-SHA `bin/ci gate` runs these package checks, including the released bounded-retry and tool-hook paths alongside the fork's synthetic-prompt cases.

Publication of server source does not replace the separately staged coding-agents package. Its supported installer `update` refreshes runtime files without rewiring host configurations. Verify both the staged bundle and a new hook process before claiming adoption.

`src/core/session-start.test.ts` and `src/core/hindsight.test.ts` cover document-presence evidence and retrieval eligibility. `src/core/runtime.test.ts` proves that an empty page roster preserves first-prompt retrieval and the existing once-per-session injection. Run these regressions and the full coding-agents package suite when adopting or retiring the startup correction. Installed Claude and Codex host canaries remain separate adoption proof.

## Adopt Or Retire

Retire this divergence when an upstream release passes the synthetic-prompt regressions through both direct `buildHookOutput` and native hook entry points. Retain the upstream two-attempt failure budget and unchanged explicit reflection path. No new filter, retry manager or logger is needed.
