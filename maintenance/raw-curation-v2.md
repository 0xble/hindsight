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
  2000 history rows. Snapshot plus unique document/chunk content is at most 8 MiB.
  Each source document and chunk is at most 1 MiB. Reject transitive observations,
  missing provenance and cross-bank dependencies. Recheck foreign links and
  observations under the same phase-2 apply and revert locks, before mutation.
- Corrections retain entity associations. Provider work happens outside pooled
  connections and locks. Fixed-order NOWAIT table locks protect closure CAS and
  atomic apply/revert. Do not add advisory locks or ordinary postcommit hooks.
- Persist consolidation, graph and model-refresh debt in the durable receipt.
  The protocol does not submit ordinary jobs. The caller coordinates maintenance
  with the consolidation owner after the curation window.
- Indexed pins protect identities and cooccurrences from already queued pruning.
  Pins remain until successful revert. Active capsules prevent bank deletion.
  Shared mention counters change only by the batch's posting delta. Compatible
  unrelated references must not conflict solely because their counters changed.
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
