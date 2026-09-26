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
    pre-call token check that triggers adaptive splitting.
  - Deterministic failures: upstream lacks the context-limit marker classifier
    and does not classify `MentalModelRefreshError` as non-retryable.
  - Scoring fence: upstream lacks the materialized candidate-source boundary
    before observation scoring.
  - Schema validation: upstream tests property `type` membership without
    rejecting non-string values first, allowing unhashable types to escape as
    `TypeError` instead of validation errors.


Run the API-local regression commands from `hindsight-api-slim`.
