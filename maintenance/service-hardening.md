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

Upstream source-edit validation (#4831) now runs within the fork's retried apply
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

Run the API-local regression commands from `hindsight-api-slim`.
