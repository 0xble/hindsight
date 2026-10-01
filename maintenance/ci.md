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
- **Surfaces:** `.github/workflows/`, `scripts/ci/validate_fork_workflows.py`,
  `bin/ci`, `.githooks/pre-push`
- **Behavior:** The exact six-workflow inventory uses only read-only permissions
  and standard runners. Publishing, release, and deployment command checks cover
  step `run`, step `shell`, and workflow/job `defaults.run.shell`, including
  authored defaults overridden by a safer shell. Automatic CI covers active
  patch regressions, lint, types, and package/import smoke tests; Windows and
  performance checks are manual-only.
  The repository CI contract lives in `bin/ci`: `gate.yml` runs `./bin/ci gate`
  on the exact PR head and its `qualification` job is the only required status
  check on `main`; `nightly.yml` runs `./bin/ci nightly` (the full offline suite)
  on a fixed daily schedule; `.githooks/pre-push` runs the bypassable
  `./bin/ci preflight`. The validator pins both trigger sets exactly.
  The patch-regression gate includes refresh-outcome and bank-template
  roundtrip tests alongside the other active service-hardening suites.
  `fork-policy.yml` uses `pull_request_target` only to run default-branch policy
  code against an immutable candidate checkout, without persisted credentials or
  candidate actions, scripts, manifests, or hooks. `qualification` is the sole
  branch-protection required check. This sync job must verify that the trusted
  `Fork Workflow Policy` `policy` check succeeded for the exact PR head SHA
  before every merge. A manual merge could omit that check: this is an accepted
  residual risk of the qualification-only protection boundary, not a claim that
  branch protection independently enforces the trusted policy.
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

