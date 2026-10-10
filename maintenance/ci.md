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
- **Behavior:** The exact six-workflow inventory uses standard runners and a
  fail-closed capability boundary as the authoritative publish ban. Every
  workflow requires an explicit top-level `permissions` mapping containing only
  `read` or `none`; every authored job-level block has the same rule. Jobs without
  a block inherit the workflow permissions. Missing workflow blocks, nulls,
  non-mappings, `read-all`, `write-all`, and every scope with `write` are rejected,
  including `packages` and `id-token`. There are no write exceptions.
  The only permitted secret reference is literal `secrets.GITHUB_TOKEN`, whose
  authority is read-only under this rule. Every other secret, dynamic
  `secrets[...]`, whole-context access such as `toJSON(secrets)`, and authored
  `secrets` blocks (including `inherit`) are rejected. Deployment environments
  remain forbidden. The positive step-action allowlist rejects known publishing
  actions and every unreviewed/local action, regardless of version or inputs;
  all reusable workflow calls are forbidden, even to a governed workflow. There
  are no credential or reusable-workflow exceptions. All six workflows currently
  use `permissions: {contents: read}`, need no secrets or write scope, and make
  no reusable-workflow calls. No workflow edits are required.
  With no write permission, no publishing credentials beyond a read-only
  GITHUB_TOKEN, no OIDC token, and no publishing actions, publication using the
  governed workflow's credentials cannot succeed however command text is written.
  Command-text checks are best-effort defense in depth, not the authoritative
  guarantee or a complete shell parser. Bypasses of that layer alone are not
  policy failures. They cover step `run`, step `shell`, and workflow/job
  `defaults.run.shell`, including authored defaults overridden by a safer shell.
  Automatic CI covers active patch regressions, lint, types, and package/import
  smoke tests; Windows and performance checks are manual-only.
  The repository CI contract lives in `bin/ci`: `gate.yml` runs `./bin/ci gate`
  on the exact PR head and its `qualification` job is the only required status
  check on `main`; `nightly.yml` runs `./bin/ci nightly` (the full offline suite)
  on a fixed daily schedule; `.githooks/pre-push` runs the bypassable
  `./bin/ci preflight`. The validator pins both trigger sets exactly, permitting
  only the legacy PR trigger or the explicit opened/synchronize/reopened/
  ready-for-review PR event set for a subsequent draft-until-ready queue rollout.
  The compatibility policy must land before that workflow change so the trusted
  default-branch validator can approve it; no push trigger or path filter is added.
  Mergify's `.mergify.yml` queue is source-controlled merge policy for this
  public fork. Its admission, merge, and auto-merge condition sets all require
  `base = main`, `-draft`, both `qualification` and trusted `policy`,
  `author = 0xble`, and `head-repo-full-name = 0xble/hindsight`. The latter two
  conditions restrict automatic queueing and fast-forwarding to the owner's
  branches; outside contributors still need explicit human review and a
  separately authorized merge path. `check-success = policy` is a bare
  check-name match, so this owner/head-repository restriction removes the
  outside-PR path rather than pretending the check name identifies its publisher.
  The queue folds up to five eligible PRs into a draft batch and fast-forwards
  `main` only after that exact batch head passes both `qualification` and the
  trusted `policy` check. Ordinary drafts skip the full gate and fail
  qualification; only draft PRs authored by `mergify[bot]` with a
  `mergify/merge-queue/` head branch run it. Preserve that conjunction, exact-head
  checkout, six-workflow inventory, and the capability boundary below.
  The patch-regression gate includes refresh-outcome and bank-template
  roundtrip tests alongside the other active service-hardening suites. The lane
  benchmark's deterministic bounded-concurrency cases are non-slow, so the gate's
  offline marker filter executes them; do not register only deselected tests.
  The Go client selector includes all `TestOperationResponseDetails` cases,
  including direct-null and invalid-discriminator coverage.
  Test sessions and every `bin/ci` profile refuse production port 5436 and
  protected test port 5556 before native migrations or connections, including
  Unix-domain PostgreSQL socket paths. Preserve `scripts/ci/test_db_guard.py`,
  the root/API pytest boundaries, and both database-safety regression suites.
  Serial tests default to disposable pg0 port 5557 (`HINDSIGHT_TEST_PG_PORT`
  can select another safe port); xdist workers explicitly select ephemeral
  ports under the startup lock rather than pg0's 5432-based allocator.
  Runtime refusals must report ordinary pytest errors, never exit workers.
  Validate inherited `PGPORT`/`PGHOST` fallback settings before engine startup
  and migration-child dispatch, even without a `db_url` or API URL override.
  Refuse libpq service settings/files rather than trusting hidden endpoints.
  The root and direct-API pytest scopes share driver guards: validate native
  psycopg2 DSNs/kwargs and asyncpg DSNs/kwargs before libpq/resolver work. Socket
  audit hooks inspect only the actual endpoint; never monkeypatch sockets or
  apply database-environment policy to unrelated xdist IPC.
  `fork-policy.yml` uses `pull_request_target` only to run default-branch policy
  code against an immutable candidate checkout, without persisted credentials or
  candidate actions, scripts, manifests, or hooks. `qualification` is the sole
  branch-protection required check. The gate's qualification also queries the
  latest completed `Repository nightly` run on `main` with the read-only Actions
  token and fails closed when that run is red or the query cannot be verified. A
  PR carrying the `nightly-repair` label is the deliberate escape hatch so a
  repair can merge; only users with write access can apply that label. This is
  an accepted residual of the qualification-only protection boundary. The guard
  depends on at least one completed nightly run; between nightly runs, merges
  rely on the normal exact-SHA gate. The gate re-runs for `labeled` and
  `unlabeled` pull-request events. The trusted `Fork Workflow Policy` `policy`
  check must still succeed for the exact PR head SHA before every merge. A manual
  merge could omit that check: this is an accepted residual risk of the
  qualification-only protection boundary, not a claim that branch protection
  independently enforces the trusted policy.
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
- **Follow-up M-a/M-b disposition:** Fail closed on the class of unquoted shell
  expansion or metacharacter syntax in every executable and known publisher
  subcommand position, not just variable wrapper commands: `$`, backticks,
  `*`, `?`, `[`, `{`, `}`, leading `~`, backslash escapes, and process
  substitutions. Preserve authored quote provenance through the existing shlex
  tokenizer rather than matching only its decoded words. The fail-closed
  backstop below supersedes the earlier exception for continuations between words.
  Parenthesized command groups and conditional/loop introducers cannot hide the
  executable position; fixed shell test syntax remains syntax, not expansion.
  Double-quoted parameter
  substitutions still expand and are forbidden; quoted literal command words
  remain subject to the publication denylist. Apply the same rule to every
  `--output`/`-o` exporter value, including attached and repeated options, Docker
  build/buildx/builder spellings and standalone buildx. Literal `type=docker`,
  `type=local,dest=out`, and `-o out` remain allowed, as do quoted literal local
  destinations containing metacharacters. Do not whitelist individual brace
  expansion forms: shell expansion in these authority-bearing positions is
  unprovable, even when an example happens to resolve to a safe verb. Ordinary
  data operands after a literal safe verb retain the existing exemptions.
  The expansion/provenance regressions exposed the class before repair. The
  owning workflow-boundary test covers all six reported brace bypasses plus
  escapes, tilde and backticks.
- **Best-effort text backstop (follow-up H):** Three reviews found shell-parser
  mismatches, most recently double-quoted split spellings of `uv`, `publish`,
  and `type=registry`. Stop relying on exact shell emulation: reject a backslash
  immediately followed by LF or CRLF in every `run`, step `shell`, and
  workflow/job `defaults.run.shell`, regardless of quoting, comments, or command.
  Use YAML multiline block scalars or shell arrays without continuations instead.
  In addition to all existing precise checks, remove quote characters,
  backslashes, dollar signs, braces, brackets, and glob characters, then collapse
  whitespace and scan the entire normalized text for registered publisher/deployer token
  sequences, `buildx ... --push`, `type=registry`, and enabled push attributes.
  A bare `publish`/`push` adjacent to a registered tool in either order also
  rejects. Comments and quoted data intentionally have no exemption from this
  coarse scan, even when they merely describe forbidden commands; rewrite that
  prose rather than weakening the rule. Ordinary build/test verbs, local
  exporters, and `--push=false` retain coverage. All six repository workflows
  pass unchanged; none uses continuations and no exemption was needed.
- **Mid-word hash and dollar quoting (follow-up H/M/L):** Mask `#` comments only
  at word boundaries before shlex tokenization; a mid-word `#` is literal as in
  Bash. Scan the whole raw word for expansion evidence, including text after
  `#`. The normalized backstop removes `$` so `$'...'` and `$"..."` cannot hide
  registered literal sequences. Preserve ordinary comments, quoted hashes, safe
  build/test verbs, and literal local exporters. These repairs improve the
  best-effort layer without making it a complete shell parser.
- **Proof and limits:** The 58-test suite at `cc2f069` preceded the capability
  amendment. Its six mid-word-hash/dollar-quote reviewer repros were accepted;
  all now reject. The expanded 70-test suite covers write at workflow/job levels
  for every permission scope (and a future-scope control), explicit mapping
  requirements, read/none and inherited-job controls, credential forms, publishing
  actions at version/branch/SHA refs, and local/remote reusable calls. Opaque
  script controls show capability rejection does not depend on shell matching.
  Normalization grids exercise `$'...'` and `$"..."` independently across
  registered tools/verbs, alongside quoted-continuation LF/CRLF and shell-scope
  coverage. All six repository workflows pass both the candidate validator and
  the trusted `origin/main` validator with zero false positives; workflow bytes,
  including trusted policy configuration and the perf fingerprint, are unchanged.
  The validator is not a sandbox for arbitrary candidate scripts or hard-coded,
  externally obtained credentials. Keep the qualification-only protection and
  exact-head trusted-policy pre-merge requirement above; the parent checks that
  policy result. Manual merges that skip it remain the explicitly accepted
  residual risk. This repair changes no workflows, protection settings, or
  runtime behavior.

