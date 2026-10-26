# Bounded Raw Curation V2

Part of the root [maintenance contract](../MAINTENANCE.md).

## Intentional Divergence

Upstream individual invalidation and restore regenerate observations. This
opt-in PostgreSQL protocol instead keeps a durable, bounded before/after capsule
for conditional recovery of raw facts and their one-hop derived observations.
It preserves exact memory, entity, association and history identities and the
captured vectors while the schema remains compatible. Ordinary curation is
unchanged. Logical bank transfer is not capsule backup.

## Boundaries

- Require an explicitly paused bank and no pending or processing consolidation.
  Pause through the bank configuration API so the configuration cache agrees.
- Support SQL-owned PostgreSQL memories only. Reject other stores and Oracle.
- Limit one batch to 50 raw targets, 200 one-hop observations, 500 co-sources or
  graph peers, 2000 entity pins/postings, 4000 incident links/cooccurrences and
  2000 history rows. Snapshot plus unique target document/chunk content is at most
  8 MiB. Each target source document and chunk is at most 1 MiB. Target provenance
  must exist. Co-sources and link-only peers are protected by their exact row
  snapshots, not by source-content revisions or source-byte accounting: peer
  documents are never rewritten, and co-sources only have consolidation timestamps
  reset and restored from their row snapshot. Apply and revert recapture with the
  source set recorded in the capsule, so capsules applied before this narrowing
  (which bound every raw closure row) still revert. Reject transitive observations and
  cross-bank dependencies. Recheck foreign links and
  observations under the same phase-2 apply and revert locks, before mutation.
- Corrections retain entity associations. Provider work happens outside pooled
  connections and locks. A bank-keyed advisory transaction lock serializes
  curation operations; fixed-order `FOR UPDATE NOWAIT` row locks protect the
  captured closure during CAS and atomic apply/revert. Row locks are bank-scoped,
  so unrelated-bank DML remains unblocked. Do not replace these locks with
  ordinary postcommit hooks.
- Persist consolidation, graph and model-refresh debt in the durable receipt.
  The protocol does not submit ordinary jobs. The caller coordinates maintenance
  with the consolidation owner after the curation window.
- Indexed pins protect identities and cooccurrences from already queued pruning.
  The 2000-pin bound includes both endpoints of captured incident cooccurrences;
  preview and receipt entity inventories count that complete pin set. Recheck
  this bound during phase-2 capture before admission or mutation.
  Pins remain until successful revert. Active capsules prevent bank deletion.
  Shared mention counters change only by the batch's posting delta. Compatible
  unrelated references must not conflict solely because their counters changed.
- Per-memory validators gate both apply and revert. Correction revert is a field
  edit with the original capsule text, even for context/date-only corrections.
  Invalidation revert keeps ordinary archive-restoration semantics.
- Reserve the exact case-sensitive batch ID `preview` for the preview route.
- Revert compares committed closure, provenance, dependencies and schema before
  restoring exact rows/history/postings/links. Drift or identity collisions fail
  atomically with 409 and keep the capsule and pins. It is conditional recovery,
  not permission to overwrite later consolidation or edits.
- Preserve PostgreSQL JSONB numbers losslessly through capture, deterministic
  hashing, capsule persistence/loading and reconstruction. Fractional numbers
  must never pass through binary floats or become quoted numeric strings.
  Verify high-precision fractions, large integers and 20th-decimal drift against
  PostgreSQL's own JSONB text, not a potentially rounded snapshot.

## Backup And Rollback

Before live batches and schema changes, take and verify a database backup with
explicit PostgreSQL connection variables. Include `curation_batches` and
`curation_entity_pins` in admin backup/restore. Capsules are deliberately excluded
from logical export, clone and replay, which regenerate IDs and embeddings.
Preserve the original database backup for conditional recovery.

A schema downgrade refuses applied capsules. Revert each accepted capsule before
retiring this migration. Do not delete capsules/pins, discard the backup or
force a downgrade to make the guard pass. Keep API/source rollback separate
from restoring live data.

## Upstream Alignment

The review fixes follow upstream guidance at
`017b3f5d888d67341e70f43c102cb8155bb9f51e` (root `AGENTS.md`, `CLAUDE.md`
and `CONTRIBUTING.md`). The independent proposal was to reuse the existing
per-memory validator, cap the complete pin set, and reserve the static preview
route's ID without replacing the protocol or its routes.

Upstream [#4889](https://github.com/vectorize-io/hindsight/pull/4889) establishes
`text`, `state` and `edits_fields` as the curation permission boundary.
[#1951](https://github.com/vectorize-io/hindsight/issues/1951) explicitly treats
ordinary recovery as state-level rather than byte-level. These fixes preserve
both contracts; exact bounded capsule recovery remains the intentional fork
extension described above.

## Batch authorization qualification

Batch curation is a bank-wide, SQL-owned PostgreSQL admin protocol: its closure,
entity pins and durable receipts cannot be narrowed to a caller's tags. At the
common `_curation_v2_store` boundary, reuse `_refuse_bank_wide_if_scoped` to reject
any nonempty read scope or restricted write scope (including `[]`, which permits
no writes) with the existing `OperationValidationError`/HTTP 403 before resolving
storage, reading a receipt, locking, or embedding. Apply and revert also invoke
`_require_writable` before backend acquisition. Unscoped callers retain the same
API and behavior; internal work retains the existing scope-resolver semantics.

`tests/test_curation_batch_authorization.py` exercises preview, get, apply and
revert for read-scoped, write-scoped and read-only-write callers, with storage,
backend and provider tripwires; it also proves all four unscoped operations pass
the boundary with and without a validator and that both writes call the writable
guard. The gate registers this regression alongside the existing real-PostgreSQL
batch and HTTP authorization suites. The fork adds no new authorization model,
request fields, or storage subsystem. On `aa7d9724f`, all 12 scope-denial cases
reach the storage tripwire and all four write-guard checks fail; all 20 cases pass
on the repair. Existing real-PostgreSQL batch/HTTP regressions still qualify in
the exact-SHA gate.

## SQL-Dialect Qualification

At frozen upstream release `v0.10.3`
(`eb6df499d35300e5b2f3f029b2e6adda04ed90f8`), the fork's graph maintenance
passes incorrectly applied PostgreSQL's curation advisory SQL to Oracle.
Gate only that bank lock by `conn.backend_type == "postgresql"` in both
`relink_pass` and `entity_prune_pass`. Oracle continues through its existing
queue-claim/prune dialect; curation v2 still rejects Oracle. PostgreSQL retains
bank-level exclusion before any claim, including the busy/not-drained result.

`tests/test_graph_maintenance_dialect.py` covers both passes, rejecting any
PostgreSQL advisory SQL on Oracle and proving PostgreSQL busy/success paths.
Before the repair its two Oracle cases failed while all four PostgreSQL cases
passed; afterward all six passed. The current focused API run passes 190 tests
with no skips or expected failures.
These dialect probes do not claim a live Oracle run.
`tests/test_curation_batch.py` retains real PostgreSQL curation-lock exclusion
and pin/prune coverage. This is a fork-only protocol integration repair, not an
Oracle curation feature. The independent proposal preceded upstream issue
searches; ordinary upstream curation [#4889](https://github.com/vectorize-io/hindsight/pull/4889)
does not supply the fork's curation-bank advisory protocol. No new contribution
is authorized. Reconcile or retire this gate only with the protocol's dialect
boundary; source rollback must not release capsules or pins.

Rust client qualification separately disambiguates inline status enum titles
in `hindsight-clients/rust/build.rs`. Receipt `applied|reverted` must not alias
ordinary operation states; curation field omission/null serialization and
readable 409 error status/body remain unchanged. See [typed failures](ocr.md)
and Rust `tests/operation_enum_collisions.rs`/`tests/curation_fields.rs`.

## Verification And Retirement

Run `tests/test_curation_batch.py`, migration-shape, admin backup/restore and
bank-transfer schema coverage. Run system story
`tests/test_18_raw_curation_batches.py` through the published Python client,
real API/worker and deterministic provider transports. The test database can be
selected with `HINDSIGHT_SYSTEM_TEST_PG_INSTANCE` and
`HINDSIGHT_SYSTEM_TEST_PG_PORT`.

Regenerate OpenAPI and Python, TypeScript, Go and Rust clients after public
contract changes. Preserve their regex/UUID runtime dependencies and explicit
nullable-field serialization. Never infer live safety from synthetic proofs.

Before adopting an upstream equivalent, compare source ownership, closure bounds,
pin/prune interaction, shared-counter deltas, exact history identity, tenant
validation, lost-ack receipts and conflict preservation. Retire this divergence
only when all those guarantees are available upstream, or its use is explicitly
withdrawn and no applied capsules remain.
