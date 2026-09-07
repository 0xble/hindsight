# Maintenance

## Relationship and baseline

Maintained fork: `0xble/hindsight` of `vectorize-io/hindsight`, branch `main`.
Canonical checkout: `/Users/brianle/Repos/hindsight`. Accepted upstream baseline:
`9e6d9e76cc52e918b24440355759991722a758a2`. Publish only to `origin`; never
push to `upstream`. Source synchronization, publication, installation, and
runtime activation are separate stages.

## Preserve

- Reject unusable OCR before it becomes memory evidence while preserving useful
  sparse, multilingual, and partially uncertain text and parser fallback.
- Preserve bounded consolidation, failure isolation, typed response validation,
  and database-operation hardening carried forward from the prior live source.
- Keep fork Actions limited to read-only, standard-runner CI and explicit manual
  smoke entrypoints; upstream deployment, signing, release, and publishing stay absent.

## Active patches

### HINDSIGHT-001: OCR evidence admission

- **Status:** Active
- **Commits:** `2fa19ab`, `83d7ad3`, `8c107c6`, `19357a9`, `b684912`, `60128d9`
- **Surfaces:** `engine/parsers/{__init__,ocr_quality}.py`, `tests/test_ocr_quality.py`
- **Upstream issue:** https://github.com/vectorize-io/hindsight/issues/3897
- **Upstream PR:** None after checked 2026-09-03
- **Regression:** `uv run --frozen pytest tests/test_ocr_quality.py`
- **Rollback:** Revert the listed commits in reverse order and rerun the regression.
- **Retire when:** A released upstream build provides equivalent admission,
  fallback, privacy, sparse-text, and multilingual behavior and passes this test.

### HINDSIGHT-002: Preserve live service hardening

- **Status:** Active
- **Commits:** `1a7e48c` (`fix: preserve live hardening on fork upgrade`),
  `ae25d02` (`fix: preserve materialized observation scoring after sync`)
- **Surfaces:** consolidation, PostgreSQL operations, structured output, config,
  monitoring documentation, and their focused tests
- **Upstream issue:** None after checked 2026-09-03
- **Upstream PR:** None after checked 2026-09-03
- **Regression:** `uv run --frozen --extra all pytest tests/test_consolidation_failure_isolation.py tests/test_consolidation_prompt_budget.py tests/test_db_abstraction.py tests/test_response_schema_validation.py`
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

### HINDSIGHT-003: Fork-owned CI governance

- **Status:** Active
- **Surfaces:** `.github/workflows/`, `scripts/ci/validate_fork_workflows.py`
- **Behavior:** The exact four-workflow inventory uses only read-only permissions
  and standard runners. Automatic CI covers active patch regressions, lint, types,
  and package/import smoke tests; Windows and performance checks are manual-only.
  `fork-policy.yml` uses `pull_request_target` only to run default-branch policy
  code against an immutable candidate checkout, without persisted credentials or
  candidate actions, scripts, manifests, or hooks.
- **Regression:** `uv run --directory hindsight-api-slim --frozen python ../tests/ci/test_validate_fork_workflows.py && uv run --directory hindsight-api-slim --frozen python ../scripts/ci/validate_fork_workflows.py`
- **Retire when:** This repository is no longer a maintained fork or assumes
  explicit ownership of deployment and publication infrastructure.

### HINDSIGHT-004: Structured OCR terminal failures

- **Status:** Active
- **Commits:** `1fe5fce`, `f405858`, `fd7fca5`
- **Surfaces:** API operation-detail models/status persistence, checked-in OpenAPI
  contracts, generated Python/TypeScript/Go clients, and `scripts/generate-clients.sh`
- **Behavior:** Failed `file_convert_retain` operations expose a stable, discriminated
  `low_quality_ocr` detail with the OCR quality reason, so callers can settle
  deterministic evidence exclusions without parsing error prose. Retries clear
  stale terminal details, and generated clients accept both supported detail types.
- **Upstream issue:** None after checked 2026-09-03
- **Upstream PR:** None after checked 2026-09-03
- **Regression:** `uv run --frozen pytest tests/test_operation_status.py`; generated
  client discriminator tests in `hindsight-clients/{python,go}`; and a successful
  `./scripts/generate-openapi.sh && ./scripts/generate-clients.sh` run.
- **Rollback:** Revert the HINDSIGHT-004 commits and restore callers to treating
  all file-conversion failures as non-terminal.
- **Retire when:** A released upstream build exposes an equivalent stable typed
  terminal failure contract for low-quality OCR.

### HINDSIGHT-005: Generated-language integrity

- **Status:** Active
- **Commits:** `ac28c9a`, `662064e`, `9f9130d`, `01fe0e5`, `e937bd6`, `c695c32`
- **Surfaces:** generated-language source profiling, retain extraction,
  consolidation, configuration, metrics, multilingual documentation, and focused
  language-integrity tests
- **Behavior:** Conservatively profile source language once outside async hot paths,
  detect confident generated drift with maintained `py3langid`, and emit bounded
  metrics in the default `observe` mode. Operators may select `retry` to add generic
  source-language guidance, regenerate once, and then preserve availability by
  accepting a persistent mismatch. Explicit fail-closed `reject` leaves source facts
  unmodified and eligible for an operator-controlled retry. `off` disables the guard.
  Retain Batch API remains available in `off` and `observe`; `retry` and `reject`
  route through the live provider path so enforcement cannot be bypassed by batch results.
  The guard abstains on short, ambiguous, materially multilingual, and unsupported
  same-script inputs. Before aggregate generated-language abstention, a dependency-free,
  source-relative script check catches substantial unquoted non-Latin prose absent from
  a source with minimum Latin evidence while preserving copied quotations, literal code,
  short names, and legitimate non-English sources. Source code literals remain
  available as evidence when a generated fact restates their values in prose.
  Bounded source-backed affix matching preserves word-script name inflections
  without raising the prose threshold or applying stem matching to CJK.
- **Upstream issue:** [#4016](https://github.com/vectorize-io/hindsight/issues/4016),
  closed as not planned after checked 2026-09-04
- **Upstream PR:** Direct predecessor
  [#4018](https://github.com/vectorize-io/hindsight/pull/4018), closed unmerged after
  maintainer review; this implementation replaces rather than extends that design
- **Regression:** `uv run --frozen pytest tests/test_language_integrity.py tests/test_language_integrity_retain.py tests/test_consolidation_retry_budget.py tests/test_fact_extraction_retry.py`
- **Rollback:** Set `HINDSIGHT_API_LLM_LANGUAGE_INTEGRITY=off` immediately, then
  revert the HINDSIGHT-005 patch stack and remove `py3langid` from the lockfile.
- **Retire when:** A released upstream build enforces an equivalent configurable,
  non-destructive-by-default language-integrity policy and passes these focused
  regressions.

## Upstream-owned recovery

Codex quota deferral is provided by upstream [#4161](https://github.com/vectorize-io/hindsight/pull/4161)
(`31da16737d98e808e21c4708379eee8c52aff546`). Processing-operation cancellation
is provided by [#4177](https://github.com/vectorize-io/hindsight/pull/4177)
(`b1de1b941857c62eb4963185450e93dcee96be70`). Both are included in the accepted
baseline. Keep these paths upstream-owned rather than adding parallel fork fixes.

- A validated provider quota-reset time defers work. Previously failed facts are
  not automatically recovered by adopting this code. Diagnose and recover those
  separately through the supported operation or consolidation APIs.
- Cancel pending or processing operations through the normal cancellation API,
  not direct database status updates. Cancellation is cooperative. Read back the
  operation and affected batch parent before declaring recovery complete.
- Per-bank consolidation claim serialization is already upstream-owned and was
  omitted from the original HINDSIGHT-002 port. Do not restore that local layer.
- Source inclusion is not runtime activation. Verify installed source identity,
  migrations, health, and recovery behavior before claiming a service adopted it.

Fork CI runs the upstream Codex/provider quota-deferral, cancellation, worker,
and operation-status regressions alongside retained-patch regressions.

## Update and verify

Fetch `origin` and `upstream`, reconcile onto current `upstream/main`, and update
this file in the same commit as any patch addition, change, or retirement.
Missing or stale patch coverage blocks publication. From `hindsight-api-slim`, run
the focused regressions plus `uv run --frozen ruff check .`,
`uv run --frozen ruff format --check .`, `uv run --frozen ty check hindsight_api`,
and the HINDSIGHT-003 regression from the repository root.
Require zero upstream-only commits, local/`origin/main` SHA parity after authorized
publication, and exact installed/runtime SHA proof when those stages are in scope.
