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
  roundtrip tests alongside the other active service-hardening suites. The lane
  benchmark's deterministic bounded-concurrency cases are non-slow, so the gate's
  offline marker filter executes them; do not register only deselected tests.
  The Go client selector includes all `TestOperationResponseDetails` cases,
  including direct-null and invalid-discriminator coverage.
  `fork-policy.yml` uses `pull_request_target` only to run default-branch policy
  code against an immutable candidate checkout, without persisted credentials or
  candidate actions, scripts, manifests, or hooks. `qualification` is the sole
  branch-protection required check. This sync job must verify that the trusted
  `Fork Workflow Policy` `policy` check succeeded for the exact PR head SHA
  before every merge. A manual merge could omit that check: this is an accepted
  residual risk of the qualification-only protection boundary, not a claim that
  branch protection independently enforces the trusted policy.
- **Upstream issue:** None for the fork publisher-policy bypasses after checked 2026-10-01.
- **Upstream PR:** None for the fork publisher-policy bypasses after checked 2026-10-01.
- **Regression:** `uv run --directory hindsight-api-slim --frozen python ../tests/ci/test_validate_fork_workflows.py && uv run --directory hindsight-api-slim --frozen python ../scripts/ci/validate_fork_workflows.py`
- **Rollback:** Restore only the HINDSIGHT-003 surfaces to the vetted fork-only
  workflow set established by `0ac5ba463940618d25781a7ac765bdeec64a9f33`, then
  preserve or reapply the later listed fork-boundary, language-regression, and
  upstream-recovery guards required for the validator to pass. Do not revert to
  inherited upstream workflow files or enable deployment, signing, release, or
  publishing. Run the HINDSIGHT-003 regression after the source-only rollback.
- **Retire when:** This repository is no longer a maintained fork or assumes
  explicit ownership of deployment and publication infrastructure.

### Publisher-policy hardening: M4 and M5

- **Origin:** Both defects are fork-owned, not regressions in unmodified upstream
  `v0.10.2` (`5fc4ce20917b916240cef27c212c387a177f115b`). The validator and its
  tests are absent there and at pinned upstream `main`
  `d863f78aa24408583d69bbc32203649fc6fc230a`. The affected literal publisher
  matching and buildx `--push` check came from fork commit
  `7e49366f2fc5123de01392a0c59738300bce6321`; both bypasses reproduce at
  `d7554cfd1f79cd000b64ea2e1565fae629f73779`.
- **Guidance and independent proposal:** Read upstream `AGENTS.md`, `CLAUDE.md`,
  `CONTRIBUTING.md`, and the referenced code-review standards at the pinned main
  revision. Before searching related work, propose extending the existing static
  validator: reject unresolved publisher subcommands and parse buildx exporters,
  while retaining build/test data arguments. Do not execute candidate commands
  or add a second workflow-policy owner.
- **Related upstream work:** Searches across open/closed issues and PRs for
  `validate_fork_workflows`, workflow policy, buildx, and CI publishing found no
  equivalent fork restriction. Merged [#1495](https://github.com/vectorize-io/hindsight/pull/1495)
  owns upstream image signing, and merged [#1491](https://github.com/vectorize-io/hindsight/pull/1491)
  adds npm publication provenance. Their discussions and diffs concern authorized
  upstream publication, not forbidding it in this fork; neither supersedes this
  patch. Keep the independent proposal as intentional fork infrastructure and do
  not submit an upstream bug fix for code upstream does not have.
- **M4 disposition:** Reject parameter, positional, array, Actions-expression,
  concatenated, and glob expansions in known publisher command paths. Recognize
  reviewed option operands before selecting a subcommand; ambiguous option arity
  with remaining expansions fails closed. Resolve wrapper executable positions
  rather than rejecting every variable argument. Literal test verbs and data
  such as `env pytest "$TESTS"` remain allowed. Extend the reviewed option sets
  alongside regression evidence when adopting new CLI syntax.
- **M5 disposition:** Inspect long and short buildx output options, including
  attached values, repeated exporters, CSV-quoted fields, and the `buildx b`
  alias. Registry outputs and enabled image push attributes are forbidden even
  with `--push=false`. Reject dynamic exporter values anywhere: a variable name
  or destination can inject CSV attributes. This conservative restriction does
  not ban variable tags, build arguments, or contexts with literal local outputs.
  [Docker's exporter contract](https://docs.docker.com/build/exporters/image-registry/)
  defines registry output as implicit `push=true` and image push-by-digest as
  publication; literal local/archive/image-without-push outputs remain available.
- **Proof and limits:** New workflow-boundary tests fail against the baseline
  before each repair; the original 37-test suite passed before adding them.
  The expanded 46-test suite and repository workflow validation pass after the
  repairs, with scoped Ruff lint/format and type checks. The validator remains
  a static workflow check, not a sandbox for arbitrary candidate scripts. Keep
  the qualification-only protection and exact-head trusted-policy pre-merge
  requirement above; the parent checks that policy result. Manual merges that
  skip it remain the explicitly accepted residual risk. This repair changes no
  workflows, protection settings, or runtime behavior.

