# Fork CI governance

Part of the root [maintenance contract](../MAINTENANCE.md).

## HINDSIGHT-003: Portable fork checks

- **Status:** Active.
- **Provenance:** `0ac5ba463940618d25781a7ac765bdeec64a9f33` established
  fork-owned checks. The portable migration replaces the four hosted workflows,
  `.local-ci.json` command string and exact-workflow allowlist with `bin/ci`.
- **Surfaces:** `bin/ci`, `scripts/ci/`, `tests/ci/`, the portable branches in
  `hindsight-api-slim/tests/conftest.py`, `ci/Dockerfile`, contribution guidance
  and optional worktree hook installation.
- **Behavior:** The repository owns dependency setup, check selection and fixtures.
  A standalone runner executes the entrypoint and publishes exact-commit results.
  GitHub uses normal PR merges. No personal CLI, credentials, shared database,
  sibling checkout, agent review receipt or central command catalog is required.
- **Regression:** `./bin/ci` on a fresh checkout, then `./bin/ci check` for edits.
  `./bin/ci list` describes the gate. The policy rejects restored Actions files,
  symlinked workflow locations and a restored legacy JSON contract.
- **Rollback:** Revert this source migration and coordinate runner enrollment with
  the corresponding source revision. Preserve enforced checks until replacement
  evidence exists. Never restore upstream release, signing or deployment jobs.
- **Retire when:** The repository is no longer maintained as a fork, or the local
  gate is replaced by another explicitly accepted contributor-portable contract.

## Maintained coverage

`REGRESSIONS` in `scripts/ci/run.py` is the executable list of the same 26 API
files previously required by the local command contract. It covers OCR and typed
failures, quota deferral and cancellation, workers and operation status,
consolidation failure/budget/atomicity/deduplication, language-integrity policies,
fact-extraction retry, content-policy failures, schemas, metrics and batch API.
The gate also covers Python/Go client operation-detail discrimination and API
Ruff lint/format, type checking, build, compile and imports.

Portable mode runs those files with `-n 0`. Tests retain their internal concurrent
operations and transaction barriers. One parent process owns a fresh PostgreSQL
cluster and checks shutdown after pytest exits. It uses the locked pg0 wheel's
CLI, private HOME and data directory, a unique instance name and a nonzero
loopback port. It verifies instance and connection identity, including the actual
data directory. Only an actual address-in-use startup failure may retry.
Normal startup, exception/SIGTERM cleanup, exited leaders with live descendants
and simultaneous independent clusters have regression coverage. Commands clean
their process group even when its leader has already exited. Database shutdown
checks process exit, allowing an exited zombie awaiting init reaping. Invocation
scratch and child TMPDIR stay under checkout-owned `.ci/runs` so database
extraction does not exhaust a runner's small `/tmp` mount. The bundled PostgreSQL and pgvector execute real migrations
and database tests.

The existing real embedding/reranking fixtures use exact local model revisions
from `scripts/ci/models.py`, CPU execution and no remote model code. Setup writes
file hashes, checks verify them and model loading is offline. Missing local ML
fails the maintained gate. The broader optional suite keeps its existing skip and
service-selection behavior outside `--portable-ci`.

The full provider/UI/integration matrix and the former manual Windows OCR/import
and performance workflows are outside this required result. Existing native OCR
tests and benchmark scripts remain available for explicitly prepared environments.
Source changes on those surfaces need their relevant extra coverage. Do not claim
Windows, live-provider or performance qualification from a green maintained gate.

## Upstream design preflight

Checked upstream at `d524904dabb98f9b5c473551fff3a597146f7da0` on 2026-09-22.
The independent design selected one invocation-owned database and pinned real
models before reviewing upstream work. Upstream
[#801](https://github.com/vectorize-io/hindsight/pull/801) removes its fixed pg0
port default but does not establish invocation ownership or checked teardown.
[#2282](https://github.com/vectorize-io/hindsight/pull/2282) resets a persistent
test schema, which is unnecessary for a fresh cluster. Neither replaces this
fork's contributor entrypoint. This CI policy is fork-specific, with no upstream
contribution PR planned.

The locked [pg0 0.15.0 CLI](https://github.com/vectorize-io/pg0/blob/v0.15.0/src/main.rs)
stores its registry and extracted installation under HOME. Its Python wrapper
suppresses stop failures, and the API's wrapper discards `data_dir` kwargs.
The harness calls the bundled CLI directly with timeouts and verifies shutdown.
The CLI invokes external `kill` for process checks. The image supplies `procps`,
and the harness checks for `kill` and `ps` before allocating a cluster. Missing
process tools must not look like a stopped database. If a successful stop leaves
the owned PID file, the existing `pg_ctl` fallback still runs. Cleanup diagnostics
preserve the original startup or test failure.
It avoids port zero because this CLI retains zero in setup/metadata even when the
underlying PostgreSQL library selects a real port. No installed library is patched.

## Runner image

`ci/Dockerfile` pins official Python 3.11, Go and uv multiarch image digests.
The [uv Docker guidance](https://docs.astral.sh/uv/guides/integration/docker/)
documents copying the standalone binaries. A trusted maintainer builds this image,
then configures a runner to execute setup and checks as a non-root user, without
an outer Docker socket or publisher credentials. Do not build candidate-provided
recipes with privileged access. The runner must qualify public-only egress with no private-network or host
reachability, credential separation and resource limits before enrollment. This
entrypoint makes model loading, Go checks and package building offline, but does
not claim that the backend disables all public egress between setup and checks. Native macOS evidence alone
does not establish that Linux runner qualification.
