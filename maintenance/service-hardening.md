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


## HINDSIGHT-008: Total OpenAI-compatible request deadline

- **Status:** Active on a branch based on deployed `ad4f7587931eec0b39607e9a4736acd09b6c63db`; source publication is not activation.
- **Behavior:** Keep the resolved per-request timeout as a wall-clock cap for structured, free-form, tool, and native Ollama requests, even when the upstream sends keepalive bytes. SDK deadline expiration remains `APITimeoutError`, so existing retry counts, backoff, and error classification do not change.
- **Adopted source:** Related upstream [#4784](https://github.com/vectorize-io/hindsight/pull/4784), merged as `878f43998dfc0e95257f8f09893d945d3da045b7`. The fork narrows that solution to the OpenAI-compatible provider and retains SDK timeout classification rather than exposing a new bare `TimeoutError` on the SDK path. No upstream publication is authorized for this delivery.
- **Configuration:** Dedup already wraps the consolidation provider via `with_config`; its distinct trace label does not select a global timeout. Preserve the regression proving consolidation `300` wins over global `120`.
- **Regression:** `tests/test_openai_total_deadline.py`, `tests/test_llm_timeout_propagation.py`, `tests/test_llm_transport_diagnostics.py`, and `tests/test_consolidation_dedup.py`.
- **Retire when:** A released upstream implementation adopted by the fork passes these regressions while preserving timeout classification and retry semantics. Do not duplicate the deadline if that implementation is reconciled.
- **Rollback:** Revert this scoped provider change and tests; no data rollback or runtime activation is part of source rollback.

Run the API-local regression commands from `hindsight-api-slim`.
