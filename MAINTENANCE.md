# Maintenance

## Relationship and baseline

Maintained fork: `0xble/hindsight` of `vectorize-io/hindsight`, branch `main`.
Canonical checkout: `/Users/brianle/Repos/hindsight`. Accepted upstream baseline:
`24cb8446b99ab2453169ef9c0779891629313ae6`. Publish only to `origin`; never
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
- **Commits:** `4671c3f`, `a3f579a`, `df35e10`, `6a9c1a6`, `54f8a08`
- **Surfaces:** `engine/parsers/{__init__,ocr_quality}.py`, `tests/test_ocr_quality.py`
- **Upstream issue:** https://github.com/vectorize-io/hindsight/issues/3897
- **Upstream PR:** None after checked 2026-08-30
- **Regression:** `uv run --frozen pytest tests/test_ocr_quality.py`
- **Rollback:** Revert the listed commits in reverse order and rerun the regression.
- **Retire when:** A released upstream build provides equivalent admission,
  fallback, privacy, sparse-text, and multilingual behavior and passes this test.

### HINDSIGHT-002: Preserve live service hardening

- **Status:** Active
- **Commit:** `002902b` (`fix: preserve live hardening on fork upgrade`)
- **Surfaces:** consolidation, PostgreSQL operations, structured output, config,
  monitoring documentation, and their focused tests
- **Upstream issue:** None after checked 2026-08-30
- **Upstream PR:** None after checked 2026-08-30
- **Regression:** `uv run --frozen --extra all pytest tests/test_consolidation_failure_isolation.py tests/test_consolidation_prompt_budget.py tests/test_db_abstraction.py tests/test_response_schema_validation.py`
- **Rollback:** Revert `002902b`; do not alter production data during source rollback.
- **Retire when:** Released upstream passes the focused regressions without this commit.

### HINDSIGHT-004: Honest document reprocessing

- **Status:** Active pending upstream release
- **Commit:** upstream `89b6a6ba2` / #3899, carried onto the current upstream baseline
- **Surfaces:** `engine/memory_engine.py`, `engine/retain/{orchestrator,types}.py`,
  `tests/test_reprocess_force_reextract.py`, and retain-parameter round-trip tests
- **Behavior:** Document reprocessing faithfully replays stored retain parameters and
  forces re-extraction rather than taking unchanged-content or recovery no-op paths.
  Ordinary unchanged retains remain optimized. The internal force flag is neither
  client-settable nor persisted.
- **Upstream PR:** https://github.com/vectorize-io/hindsight/pull/3899
- **Regression:** `uv run --frozen --extra all pytest tests/test_reprocess_force_reextract.py tests/test_retain_params_roundtrip.py`
- **Rollback:** Revert the fork commit carrying `89b6a6ba2`; do not use ordinary
  reprocessing as a substitute because it silently no-ops on identical content.
- **Retire when:** An accepted upstream baseline contains #3899 and its regressions.

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

## Update and verify

Fetch `origin` and `upstream`, reconcile onto current `upstream/main`, and update
this file in the same commit as any patch addition, change, or retirement.
Missing or stale patch coverage blocks publication. From `hindsight-api-slim`, run
the focused regressions plus `uv run --frozen ruff check .`,
`uv run --frozen ruff format --check .`, `uv run --frozen ty check hindsight_api`,
and the HINDSIGHT-003 regression from the repository root.
Require zero upstream-only commits, local/`origin/main` SHA parity after authorized
publication, and exact installed/runtime SHA proof when those stages are in scope.
