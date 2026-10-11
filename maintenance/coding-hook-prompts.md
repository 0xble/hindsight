# Coding Hook Prompts

Part of the root [maintenance contract](../MAINTENANCE.md).

## Intentional Divergence

At fork base `aa7d9724f7d6076f87d29163fd2baff1c0213595`, the coding-agents transcript reader already stripped harness transport wrappers, but automatic memory injection queried their raw contents. A synthetic first prompt could consume a session's initial injection or retry. The shared hook runtime now uses that existing tag rule for automatic queries and leaves wholly synthetic prompts outside session accounting. Explicit MCP reflection is unchanged.

Upstream guidance was inspected at `a9eeed77f9bce99d8f97d39a30d3281747300824` on `2026-10-11`, including AGENTS.md, CLAUDE.md and CONTRIBUTING.md. The independent approach was to reuse the existing structural filter before automatic query and cache work. Related [issue #3023](https://github.com/vectorize-io/hindsight/issues/3023) and merged [PR #3378](https://github.com/vectorize-io/hindsight/pull/3378) establish the same rule for retained transcripts. Merged [PR #4712](https://github.com/vectorize-io/hindsight/pull/4712) already supplies bounded retry after failed injection, so this patch preserves that implementation. Searches for synthetic hook prompts and task notifications found no automatic-query equivalent. Upstream accepts external bug reports rather than external PRs.

The previous reflection warning declared a session memory-less before retrieval fallback ran. The warning now records only the failed reflection. Existing fallback diagnostic events and the turn's notice continue to report the eventual outcome, without adding prompt or answer fields.

## Update And Verify

The maintained boundary is `hindsight-integrations/coding-agents/src/core/hook.ts`, shared by hook and persistent-plugin callers. `src/core/runtime.ts` excludes synthetic prompts before the plugin host's separate first-turn roster counter advances. `src/core/transcript-util.ts` owns the tag rule. `src/core/hook-synthetic.test.ts` drives the public hook runtime and native Claude lifecycle declaration, proving no network work or session mutation on synthetic prompts, preservation of real surrounding text, deferred new-bank prompts, later retry and truthful fallback logging. `src/core/runtime.test.ts` proves that a plugin's first real prompt receives its live page roster after synthetic prompts. The package's existing `npm test`, `npm run build` and TypeScript check cover the integration.

Publication of server source does not replace the separately staged coding-agents package. Its supported installer `update` refreshes runtime files without rewiring host configurations. Verify both the staged bundle and a new hook process before claiming adoption.

## Adopt Or Retire

Retire this divergence when an upstream release passes the synthetic-prompt regressions through both direct `buildHookOutput` and native hook entry points. Retain the upstream two-attempt failure budget and unchanged explicit reflection path. No new filter, retry manager or logger is needed.
