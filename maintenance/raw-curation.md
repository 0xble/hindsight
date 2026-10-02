# Guarded Raw Curation

Part of the root [maintenance contract](../MAINTENANCE.md).

## HINDSIGHT-010: Source-Bound Curation Preconditions

- **Status:** Active in this source candidate, runtime adoption is separate.
- **Surfaces:** optional `curation_guard` on the existing memory PATCH, the
  engine write transaction, generated OpenAPI and clients.
- **Consumer:** the personal curation controller needs to reject stale reviewed
  evidence and avoid deleting dependent observations or their history.
- **Regression:** `uv run --frozen --extra all pytest -n 0 tests/test_guarded_curation.py`.
  Run separately from concurrent database writers. The repository gate owns this stage.
- **Rollback:** revert the source patch. This does not reverse earlier fact changes.
- **Retire When:** a released upstream API offers equivalent atomic source and
  memory compare-and-set checks and protects dependent observations, passing
  these regressions.

The `raw-curation-v1` guard requires expected SHA-256 digests for the raw memory
and its source document/chunk, and literal `true` for both safety flags.
The memory projection is `CurationMemorySnapshot`. Entities and tags are sorted
before hashing. The source projection is `CurationSourceSnapshot`. JSON uses
UTF-8, sorted object keys, no whitespace and no nonfinite numbers. Clients must
match Python's numeric encoding or exclude numeric metadata and scopes.
The first personal controller excludes numbers, nonempty entities and entity edits.

The guard runs in the final write transaction, after any embedding computation.
It requires PostgreSQL, an explicit paused automatic-consolidation override,
no pending or processing consolidation for the target bank, source bodies at
most 1 MiB each, and zero dependent observations. Source drift, memory drift,
entity-name drift or lock contention returns HTTP 409 without mutation.
Guarded writes never submit post-commit automatic consolidation, regardless of
cached configuration or a pause being lifted after commit. Ordinary memory
PATCH scheduling also reads uncached configuration to honor a persisted pause.
External memory stores and entity edits are unsupported. Other unguarded PATCH
behavior is unchanged.

Array-valued observation dependencies lack a row-level foreign-key lock
discipline. The opt-in transaction therefore acquires fixed-order
`SHARE ROW EXCLUSIVE NOWAIT` table locks through commit. A transaction-local
100 ms `lock_timeout` also bounds row and foreign-key lock waits: existing
`SELECT ... FOR UPDATE` locks are compatible with the table locks but can block
later writes. PostgreSQL `55P03` from anywhere in the guarded transaction becomes
HTTP 409 after rollback, including these timeouts. The timeout does not persist
on pooled connections. This briefly blocks competing writes across banks in the
same database. Reads continue. No network
embedding occurs under these locks. The consolidation owner coordinates the
quiescent deployment and curation window. This is a deliberate fork divergence,
not a per-bank locking guarantee or server enforcement of a host-local lease.

The client owns the logged batch ID, backups, evidence, exclusive cooperative
lease, ambiguous-write reconciliation and guarded reversal. Restoring a raw fact
does not byte-restore its graph, embedding or derived state. Never treat an
unguarded API's reversible state transition as observation-history recovery.

## Upstream Assessment

Guidance was read at upstream `ec39e10900c6a971f1a73cd37402228d5cccaa25`
on 2026-10-01: root AGENTS, CLAUDE and CONTRIBUTING. The independent approach
extends the existing PATCH and its commit transaction rather than adding a
second mutation route or direct database writes.

[Curation RFC #1951](https://github.com/vectorize-io/hindsight/issues/1951)
explicitly defines reversal as state-level, with derived observations deleted
and regenerated. [Validator hooks #4889](https://github.com/vectorize-io/hindsight/pull/4889)
run before work and after commit, so they cannot hold an atomic precondition
through the write. [Consolidation source-drift fix #4893](https://github.com/vectorize-io/hindsight/pull/4893)
protects a consolidation result from stale source reads, but does not protect
the curation cascade from observation insertion. Neither replaces this guard.
No upstream synchronization or model/routing change is included.

All SDKs were regenerated. Unrelated generator drift replaced the existing
Python and Go scalar file-conversion failure-reason contracts and broke the
Python discriminator regression. Those maintained model changes were preserved
from the base revision. Only guard-related generated changes are included.
