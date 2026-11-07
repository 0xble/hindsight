# Bounded consolidation schema correction

Part of the root [maintenance contract](../MAINTENANCE.md).

## Intentional divergence

Upstream #3783 (`ad6312a512119c45a5dd67c43dccb6d391de2609`) keeps
schema validation fail-fast. This fork permits one changed-prompt, whole-response
correction for expected consolidation response action `missing`/`model_type`
errors only. Upstream #3003 closed without repair; #3137 rejected per-item salvage.
Do not replace this boundary with coercion, dropped invalid actions, defaults,
empty-response repair, routing changes, or fabricated references.

[Issue #5572](https://github.com/vectorize-io/hindsight/issues/5572) is closed as
completed by [PR #6374](https://github.com/vectorize-io/hindsight/pull/6374), merged
after the selected v1.2.0 release. That unreleased correction is not part of this
sync and does not replace the fork's job-wide completion budget or widen it.

- **Surfaces:** `consolidation/consolidator.py`, `llm_wrapper.py`,
  `llm_attempt_limit.py` under `hindsight-api-slim/hindsight_api/engine/`.
- **Bounds:** At most ten additional actual completions per 1000-fact job.
  Each round gets one credit per complete 100 configured fact slots, capped at
  ten, shared across fetches, scopes, lanes and bisection. Thus requeued default
  100-fact rounds get one credit each, not ten. Unlimited and sub-100-fact rounds
  fail closed. Allow at most one correction per subbatch.
  `HINDSIGHT_API_CONSOLIDATION_ROUND_CORRECTION_BUDGET` (bank field
  `consolidation_round_correction_budget`) is a lower ceiling, not an escape
  hatch: the effective allowance is `min(explicit, calculated)` when set.
  Zero disables corrections; negative values remain invalid. The same ceiling
  covers schema and detail corrections together. An explicit value cannot
  enable corrections for unlimited or sub-100 rounds.
  Keep model, schema, temperature, completion cap and validation unchanged.
  Reject oversized correction context before sending. Only audited OpenAI-compatible
  and Codex provider attempt boundaries support correction; others fail closed.
- **Signals:** Preserve cancellation, auth 401/403 and exact quota-reset signals.
  Hidden provider retries must not issue another correction completion. Failures
  retain normal failed-leaf/bisection behavior, with no replay of committed writes.
- **Regression:** Run `tests/test_consolidation_schema_correction.py` plus existing
  consolidation retry, failure-isolation, prompt-budget, lane/scope, language and
  provider quota/auth regressions in an explicitly isolated test database.
  Synthetic provider fixtures prove mechanics, not live provider repair efficacy.
- **v1.1.0 fixtures (2026-10-30):** The requeued-round budget fixture declares
  released `enable_auto_consolidation` so its 10 default rounds drain the backlog.
  Guard/provenance database fixtures stub `embedding_utils.generate_embeddings_batch`
  with one vector per input at the active embedder's dimension, replacing the
  removed single-observation helper. Keep every request-count, source attribution,
  hydration cap, row-growth and pending-fact assertion. A failure with complete
  released doubles falsifies fixture drift as the explanation for that case.
- **Update/retire:** Compare upstream fail-fast and provider-attempt changes before
  porting. Retire only when an upstream equivalent preserves all bounds and
  references/language/topology checks, or when correction is explicitly withdrawn.
  The fork intentionally retains per-source recall topology for UPDATE admission,
  unlike v1.1.0's pooled recall. Both reference filtering and write preparation
  enforce it, including a triage-injected update. A rejected citation stays pending
  and is not stamped consolidated. Valid siblings commit, while facts cited only
  by dropped actions remain pending for bounded retry.
  Rollback removes only these correction surfaces and this record; never reset
  failed facts or replay previously committed consolidation writes.

## v1.2.0 Fact-Verdict Adaptation

The released model answers per-fact verdicts instead of write actions. Preserve
the dynamic fact-verdict schema and cache-aware token accounting, including
update/delete work when a scope has no remaining create slots. Malformed
siblings must reject the complete response, not disappear through released
per-item salvage. Only a change with no observation target is skipped; that
cannot authorize a write. Never fabricate source ids from entry position or
turn an invented observation target into a CREATE. Valid momentary skips must
survive reference filtering. Original-chunk language checks and the detail guard
inspect every update proposal before repeated targets are collapsed.

`tests/test_consolidation_fork_verdict_contract.py` locks these protocol
boundaries alongside the existing full correction, language, provenance and
lane regressions. AST-only schema probes are not native engine qualification.

The production adaptation rejects non-string `new` siblings (including falsy
`null`, `false` and `0`) without widening correction eligibility. Zero create
capacity is carried into each fact's `new.maxItems` in both normal and strict
schemas; existing-observation work remains legal. The native provider/request
and pg0 persistence regressions verify that a valid uncited momentary skip is
stamped with or without valid write siblings while an invalid sibling's fact
remains pending. An unknown skip or a contradictory skip on the invalid action's
own source cannot settle that fact.

The native mock provider must emit the required empty `existing` list for
create-only verdicts. An incomplete mock response otherwise enters correction
and fails closed on an unaudited provider, hiding observations in unrelated
document-history and tag-invalidation tests. Preserve those full native engine
regressions and the direct dynamic-schema check, not a mock correction bypass.
