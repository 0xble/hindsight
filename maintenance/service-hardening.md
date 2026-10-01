# Live service hardening

Part of the root [maintenance contract](../MAINTENANCE.md).

Preserve bounded consolidation, failure isolation, typed response validation,
and database-operation hardening carried forward from the prior live source.
Do not replace these safeguards with the upstream recovery features identified
in the root contract.

## HINDSIGHT-002: Preserve live service hardening

- **Status:** Active
- **Commits:** `1a7e48c` (`fix: preserve live hardening on fork upgrade`),
  `ae25d02` (`fix: preserve materialized observation scoring after sync`)
- **Surfaces:** consolidation, PostgreSQL operations, structured output, config,
  monitoring documentation, and their focused tests
- **Upstream issue:** None after checked 2026-09-11
- **Upstream PR:** None after checked 2026-09-11
- **Regression:** `uv run --frozen --extra all pytest tests/test_consolidation_failure_isolation.py tests/test_consolidation_prompt_budget.py tests/test_db_abstraction.py tests/test_response_schema_validation.py tests/test_refresh_outcome_metadata.py tests/test_bank_template_full_roundtrip.py`
- **Upstream test alignment:** Upstream's refresh-outcome matrix expects a
  `MentalModelRefreshError` to be retried; the fork's copy expects the terminal
  failure instead, while other escaped refresh errors keep the generic retry.
  `consolidation_max_context_tokens` is bank-configurable, so it is also declared
  on `BankTemplateConfig` and exported with bank templates.
- **Rollback:** Revert the listed commits; do not alter production data during source rollback.
- **Retire when:** Released upstream passes the focused regressions without these commits.
- **Component assessment:** None of the remaining safeguards is replaced by quota
  deferral or cancellation at the accepted baseline:
  - Prompt budget: upstream lacks `consolidation_max_context_tokens` and the
    pre-call token check that triggers adaptive splitting. Both convenience
    clients map the bank override. A dropped model action citing only invented
    fact IDs must reject the batch rather than silently losing output.
  - Deterministic failures: upstream lacks the context-limit marker classifier
    and does not classify `MentalModelRefreshError` as non-retryable.
  - Scoring fence: upstream lacks the materialized candidate-source boundary
    before observation scoring.
  - Schema validation: upstream tests property `type` membership without
    rejecting non-string values first, allowing unhashable types to escape as
    `TypeError` instead of validation errors.

## HINDSIGHT-007: Consolidation lock ordering and deadlock retry

- **Status:** Active
- **Commits:** `27fcb7b`
- **Surfaces:** `engine/consolidation/consolidator.py` (ordered `FOR SHARE` source
  check and bounded whole-transaction retry of the apply step on deadlock),
  `engine/retain/fact_storage.py` (document-wide metadata update locks rows in
  `ORDER BY id` first), and `tests/test_consolidation_deadlock_order.py`
- **Behavior:** Consolidation's source-liveness check and retain's document-wide
  tag/scope update acquire `memory_units` row locks in the same order, so they no
  longer deadlock. The apply transaction retries a residual PostgreSQL deadlock and
  discards rolled-back results rather than failing the operation.
- **Upstream issue:** None after checked 2026-09-27
- **Upstream PR:** None after checked 2026-09-27
- **Regression:** `uv run --frozen pytest tests/test_consolidation_deadlock_order.py tests/test_consolidation_dedup.py`
- **Rollback:** Revert the listed commit; do not alter production data during source rollback.
- **Retire when:** Released upstream orders both lock paths or retries consolidation
  deadlocks, and passes this regression.

## Consolidation Adaptation At The Accepted Checkpoint

Upstream source-edit validation ([PR #4893](https://github.com/vectorize-io/hindsight/pull/4893), addressing issue #4831) now runs within the fork's retried apply
transaction. Source locks keep `ORDER BY id`, and a response computed from edited
facts writes neither observations nor consolidation stamps. Source versions come
from `StoredMemory.updated_at` on both strict and fair fetch paths.

Keep the fork's large-backlog fair selector and its opt-in configuration. The
upstream overfetch selector bounds its window to sixteen rounds, while the fork
examines up to 100,000 candidate facts. Use the upstream store helper as the
fallback without widening the configured scope filter. Lane serialization,
stale-reference recovery, atomic language validation and bounded schema correction
remain intentional divergences. Retain their existing regression suites when
upstream changes consolidation signatures or typed store models.

Append strategy retention is now upstream-owned: the append path carries the
complete caller item instead of copying individual fields (#4590). Keep only
the fork's unmatched-rechunk tail handling, exercised by
`tests/test_append_after_chunking_change.py`, alongside upstream's
`tests/test_retain_append_mode.py` coverage.


## Exact CREATE Fold Fallback And Dialect Boundary

The fork lane exact-fold snapshot can become stale after an earlier CREATE in the same response semantically rewrites its twin. A failed text CAS must fall through to the normal fold/create path; it is not durable coverage for stamping sources. Shown/reply twins and serialized exact probes must also respect the PostgreSQL-only fold SQL boundary. Exact folding stays enabled on PostgreSQL even when the semantic threshold is 1.0; Oracle preserves CREATE sources through insertion instead.

- **Surfaces:** `engine/consolidation/consolidator.py` and `tests/test_consolidation_fold_fallback.py`.
- **Provenance:** Fork lane snapshot behavior `d7024db7df`; shown/reply preparation `3a44d762c8` and `7fe8846125`.
- **Regression:** `uv run --frozen --extra all pytest tests/test_consolidation_fold_fallback.py tests/test_consolidation_scope_parallelism.py tests/test_consolidation_dedup.py` in a disposable PostgreSQL database. The Oracle case verifies dialect routing, not live Oracle execution.
- **Retire when:** Released upstream provides equivalent exact-fold fallback, dialect routing, and source-coverage guarantees.

## Lane Accounting And Regression Reliability

- **Disposition:** Fork-only correction and regression hardening, compared with upstream release `v0.10.2` (`5fc4ce20917b916240cef27c212c387a177f115b`) and pinned upstream main `d863f78aa24408583d69bbc32203649fc6fc230a`. Neither has the fork's stale-reference retries, pending-conflict counters, within-scope lane dispatcher, or these lane regressions. Do not report these findings as upstream defects.
- **Preflight:** Read upstream `AGENTS.md`, `CLAUDE.md`, `CONTRIBUTING.md` and `.claude/skills/code-review/SKILL.md` at the pinned revision. The initial proposal was to preserve committed DELETE totals and pending-leaf counts, then replace entry/sleep-based race probes with completed-read/event barriers and guaranteed task cleanup, without changing dispatch or retry policy.
- **Related upstream design:** [Issue #4063](https://github.com/vectorize-io/hindsight/issues/4063#issuecomment-5537536098) explicitly keeps same-scope calls serial for an up-to-date observation view. The fork's opt-in lanes remain an intentional throughput/safety tradeoff, with ordered apply, snapshot validation and bounded stale retries; this patch does not relax those fences. Adopted upstream [PR #4893](https://github.com/vectorize-io/hindsight/pull/4893) (`ff7f96ce23dd1c40359b95ad3242e2f4539bcef0`, already in `v0.10.2`) validates edited source facts, not observation snapshots or pending-conflict accounting. It is related prior art, not a replacement. No new upstream contribution is made: the smallest correction belongs to the existing fork lane owner, not a new upstream scheduler.
- **L1 provenance and behavior:** The exhaustion `continue` in `d145ee52c9` bypassed DELETE accumulation; `39ebfb19ff` retained earlier-scope action results but not their DELETE totals. Deferred-count initialization came from `1514828518`. Preserve committed deletions once and count distinct pending conflict IDs without stamping or failing them. The new multi-scope regression deletes a real row, exhausts three stale attempts on the later scope, and expects deleted=1/deferred=1/processed=0/failed=0 with the source still pending. RED: two counter assertions failed and the invalid-content control passed; GREEN: all three pass.
- **L3 provenance and behavior:** `d7024db7df` marked the bounded lane probe `slow`, excluding both cases from the offline gate. Keep its measurements and correctness assertions, remove that mark, and accept the current recall `config` argument. RED under the gate's marker filter: two deselected/zero selected; after inclusion, both exposed a stale callback signature. GREEN: both cases pass, taking about three seconds each in the focused qualification run.
- **L4 provenance and behavior:** Early stale-read barriers originated in `d145ee52c9` and `df8c419f27`; timing-based overlap in the fair selector came from `77c005423c`. Release recall barriers only after both/all three SQL snapshots finish, capture each call's ordinal before awaiting, and assert the original snapshots and fresh retry. Replace failed-preparation and fair-selection sleeps with event rendezvous. Drain cancelled jobs inside the patch lifetime and again before bank teardown, including injected setup failure. RED: two prematurely released read barriers fail; setup-interruption without cleanup fails; unconditional successor release fails the ordered-apply probe. GREEN: completed-read, interrupted-cleanup, ordered-apply and fair-overlap cases pass.
- **L5 provenance and behavior:** `120d5d0822` supplied the apply-time capacity guard and its insufficiently synchronized regression. Force both completed preparation counts to be zero before either batch may apply. RED with only the apply-time guard disabled: two observations exceed the cap of one. GREEN with the real guard: one observation, both sources stamped. This is stronger proof of an existing fence, not another production scheduling change.
- **Regression:** From `hindsight-api-slim`, use a disposable PostgreSQL database and run `tests/test_consolidation_scope_parallelism.py`, `tests/test_consolidation_fair_selection.py` and `tests/test_consolidation_lane_benchmark.py` with the offline gate's marker filter, plus the atomicity, fold-fallback, schema-correction, retry-budget, failure-isolation, prompt-budget, deadlock and dedup suites. Qualification used worker-isolated `pg0://qualify-r2-consolidation`, offline local models and synthetic LLM responses; the expanded run passed 206 tests. These measurements prove mechanics, not live-provider throughput or live Oracle behavior. CI wiring belongs to `maintenance/ci.md` and must continue selecting these non-slow cases.
- **Update/retire:** Reconcile this correction with any future lane, stale-snapshot or counter change together with [bounded schema correction](schema-correction.md). Retire only when released upstream supplies equivalent behavior and passes the completed-read, cancellation, counter and apply-capacity regressions. Rollback is source-only; never reset pending facts or replay earlier committed scope writes. Fork publication and independent review remain the integrating parent's responsibility.

## Offline Normalized-Index SQL

The fork-only normalized-observation index revision `e6f7a8b9c0d1` was introduced by `621dda7e65`; it is absent from release `5fc4ce20917b916240cef27c212c387a177f115b`. Its invalid-index catalog probe returned no result in offline Alembic mode and aborted SQL emission. Skip only that catalog probe during `--sql` generation; retain online interrupted-index recovery and concurrent/idempotent index DDL. `tests/test_alembic_dag.py` renders the real upgrade/downgrade using offline PostgreSQL operations for default and tenant schemas, without opening a connection. Existing online normalized-index regressions preserve recovery coverage. Upstream guidance was pinned at `d863f78aa24408583d69bbc32203649fc6fc230a`; targeted Alembic-offline search found no equivalent fix. Retire this divergence with the owning exact-CREATE fold behavior, not merely an upstream migration rename.

Run the API-local regression commands from `hindsight-api-slim`.
