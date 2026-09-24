# Fork CI governance

Part of the root [maintenance contract](../MAINTENANCE.md).

Keep the fork-owned CI boundary across upstream synchronization. This is
intentional fork infrastructure policy, not upstream deployment ownership.

## HINDSIGHT-003: Fork-owned CI governance

- **Status:** Active
- **Stable provenance:** `0ac5ba463940618d25781a7ac765bdeec64a9f33`
  (`ci: replace inherited workflows with fork checks (#1)`),
  `d5dafa7e938793bd4474e947cde4f59e2ad4eb68`
  (`ci: preserve fork workflow boundary after upstream sync`),
  `b2e1776828c13cbaf0a58513299de73b1d8e557e`
  (`ci: run language integrity regressions`), and
  `d4060b4a6f4f3faa0b043adf9157af332b0b94a1`
  (`Guard upstream recovery paths in fork CI (#9)`).
- **Surfaces:** `.github/workflows/`, `scripts/ci/validate_fork_workflows.py`
- **Behavior:** The exact four-workflow inventory uses only read-only permissions
  and standard runners. Automatic CI covers active patch regressions, lint, types,
  and package/import smoke tests; Windows and performance checks are manual-only.
  `fork-policy.yml` uses `pull_request_target` only to run default-branch policy
  code against an immutable candidate checkout, without persisted credentials or
  candidate actions, scripts, manifests, or hooks.
- **Upstream issue:** None after checked 2026-09-11
- **Upstream PR:** None after checked 2026-09-11
- **Regression:** `uv run --directory hindsight-api-slim --frozen python ../tests/ci/test_validate_fork_workflows.py && uv run --directory hindsight-api-slim --frozen python ../scripts/ci/validate_fork_workflows.py`
- **Rollback:** Restore only the HINDSIGHT-003 surfaces to the vetted fork-only
  workflow set established by `0ac5ba463940618d25781a7ac765bdeec64a9f33`, then
  preserve or reapply the later listed fork-boundary, language-regression, and
  upstream-recovery guards required for the validator to pass. Do not revert to
  inherited upstream workflow files or enable deployment, signing, release, or
  publishing. Run the HINDSIGHT-003 regression after the source-only rollback.
- **Retire when:** This repository is no longer a maintained fork or assumes
  explicit ownership of deployment and publication infrastructure.
