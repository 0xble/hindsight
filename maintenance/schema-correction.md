# Bounded consolidation schema correction

Part of the root [maintenance contract](../MAINTENANCE.md).

## Intentional divergence

Upstream #3783 (`ad6312a512119c45a5dd67c43dccb6d391de2609`) keeps
schema validation fail-fast. This fork permits one changed-prompt, whole-response
correction for expected consolidation response action `missing`/`model_type`
errors only. Upstream #3003 closed without repair; #3137 rejected per-item salvage.
Do not replace this boundary with coercion, dropped invalid actions, defaults,
empty-response repair, routing changes, or fabricated references.

- **Surfaces:** `consolidation/consolidator.py`, `llm_wrapper.py`,
  `llm_attempt_limit.py` under `hindsight-api-slim/hindsight_api/engine/`.
- **Bounds:** At most ten additional actual completions per 1000-fact job.
  Each round gets one credit per complete 100 configured fact slots, capped at
  ten, shared across fetches, scopes, lanes and bisection. Thus requeued default
  100-fact rounds get one credit each, not ten. Unlimited and sub-100-fact rounds
  fail closed. Allow at most one correction per subbatch.
  Keep model, schema, temperature, completion cap and validation unchanged.
  Reject oversized correction context before sending. Only audited OpenAI-compatible
  and Codex provider attempt boundaries support correction; others fail closed.
- **Signals:** Preserve cancellation, auth 401/403 and exact quota-reset signals.
  Hidden provider retries must not issue another correction completion. Round
  correction-credit exhaustion leaves facts pending with neither consolidated
  nor failed stamps: exclude them from further fetches in the same round and
  permit a later job to allocate a fresh budget. Include them in
  `memories_deferred`, while retaining action and DELETE counts for earlier
  committed scopes. Initialize scope results separately for every bisected leaf.
  Plain per-call attempt limits and input-shaped failures retain failed-leaf
  bisection behavior. Detail-loss credit exhaustion remains inside its additive
  preserve-separate fallback, without applying the lossy UPDATE or its DELETE.
  Deferral does not promise immediate requeue when the round slot limit was not
  reached. Adoption must not reset previously failed facts or replay committed
  writes.
- **Regression:** Run `tests/test_consolidation_schema_correction.py` plus existing
  consolidation retry, failure-isolation, prompt-budget, lane/scope, language and
  provider quota/auth regressions in an explicitly isolated test database.
  The maintained failure-isolation suite covers real PostgreSQL pending stamps,
  partial CREATE/DELETE accounting, per-leaf result isolation, strict/fair fetch
  exclusion and fresh-round eligibility. The schema-correction suite covers
  additive detail-loss fallback after either kind of credit reservation.
  Both suites are registered in `bin/ci` patch regressions.
  Synthetic provider fixtures prove mechanics, not live provider repair efficacy.
- **Update/retire:** Compare upstream fail-fast and provider-attempt changes before
  porting. Retire only when an upstream equivalent preserves all bounds and
  references/language/topology checks, or when correction is explicitly withdrawn.
  Rollback removes only these correction surfaces and this record; never reset
  failed facts or replay previously committed consolidation writes.
