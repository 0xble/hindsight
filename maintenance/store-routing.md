# SQL-Store Routing on Oracle

Part of the root [maintenance contract](../MAINTENANCE.md).

## HINDSIGHT-011: SQL-backed consolidation reads

- **Status:** Active
- **Origin:** Fork-only regression introduced by the Oracle table-routing adaptation in `faf3fa46220ffb5aeb24b3df1e3d35171f68bbd1`. The reconciled upstream store boundary used `store.store_owned_for(bank_id)` for the default SQL store; widening that predicate to Oracle made `_resolve_original_source_texts()` pass `conn=None` to the SQL-backed PostgresMemories implementation.
- **Surfaces:** `hindsight-api-slim/hindsight_api/engine/consolidation/consolidator.py`, the shared `engine/memories/pg/consolidation.py` lock helpers, and focused consolidation regressions.
- **Behavior:** Route memory-row and chunk-body reads through the store API only for store-owned banks. SQL-backed stores, including Oracle-routed SQL fixtures, acquire and pass a real connection to `get_memories()` and read chunk bodies through the SQL connection. Keep Oracle table-dialect routing for the SQL statements that require it.
- **Upstream disposition:** No upstream bug; the failing predicate was fork-only. The upstream store boundary at the reconciled checkpoint uses the store-ownership predicate for this source-read path. No upstream issue or PR applies.
- **Guidance and proposal:** Preserve the existing store owner as the source-of-truth boundary, and keep dialect selection separate from storage ownership. Do not make SQL-backed reads connectionless merely because Oracle SQL routing is active.
- **Source-fence qualification (v0.10.3):** `_memory_store_handles_tables` selects the store API's dialect path; it does not imply external storage ownership. SQL-owned Oracle now calls `store.lock_live_memory_ids` and `store.memories_changed_since` on the caller's apply connection. Preserve ordered source locks (`ORDER BY id FOR SHARE`, rewritten to Oracle `FOR UPDATE`), actual `updated_at` comparisons, deleted-source filtering and stale-batch rejection. External-store existence checks route through the same `lock_live_memory_ids` interface, retaining the default unlocked check for stores without a native fence while honoring overrides; PostgreSQL inline routing is unchanged.
- **Upstream source fence:** Frozen release `eb6df499d35300e5b2f3f029b2e6adda04ed90f8` includes [#4893](https://github.com/vectorize-io/hindsight/pull/4893), the source-edit-during-LLM-call fence. This repair preserves that adopted guarantee on the fork's Oracle routing; it is not a new backend feature or an upstream defect. Formed the correction before searching the existing upstream tracker; no new publication is authorized.
- **Regression:** From `hindsight-api-slim`, run `uv run --frozen pytest -q tests/test_consolidation_scope_parallelism.py::test_oracle_sql_store_source_read_uses_connection tests/test_consolidation_deadlock_order.py tests/test_consolidation_create_proof_count.py`. The source-read test is red when Oracle sends `conn=None` to the SQL store. `test_oracle_sql_store_preserves_source_fences` is red when Oracle skips drift detection or reads unlocked sources, and proves the shared SQL-store lock statements retain bank scope, ordering and Oracle rewrite.
- **Qualification evidence:** Both Oracle source-fence cases were red before the repair and green in the focused API run. This is deterministic SQL-store/caller-connection and Oracle-rewriter evidence, not a claim of live Oracle database qualification.
- **Scope-count qualification (v0.10.3):** Store-routed observation counts use `tags_match="all_strict"`, matching the SQL branch's `tags @> $2` containment instead of charging global/untagged observations against a tagged scope. Preserve pagination and all-tag containment (not exact-set equality); an empty requested tag list still matches all observations, as SQL containment of an empty array does. `test_store_observation_count_matches_sql_containment` covers Oracle and external-store routing, single/multi/empty tag sets, untagged rows, supersets, unrelated scopes and multiple scan pages. `test_store_owned_source_liveness_uses_store_lock_boundary` proves external native fences are not bypassed. The tagged-scope count cases are red on `aa7d9724f` (4 failures; empty-scope controls pass) and all 6 cases pass after the strict-mode correction; the external-fence regression is independently red/green.
- **Scope-count pagination (v1.2.0 review):** Exhaust the store cursor rather than returning a partial count after 100 pages (50,000 observations). Pass opaque tokens unchanged, continue through empty intermediate pages, stop only at an empty cursor, and fail explicitly on a repeated/cyclic cursor instead of reporting an incomplete capacity count. The focused `test_store_observation_count_*` matrix covers 50,501 observations on external and Oracle routing, empty/exact-page results, empty intermediate pages and cursor cycles; this is store-contract evidence, not live Oracle qualification.
- **Rollback:** Revert only the source-read/source-fence routing corrections, strict scope count, their regressions, shared lock-order changes and this record; retain the broader Oracle SQL-dialect routing required by the fork contract.
- **Retire when:** The fork no longer carries the Oracle SQL-dialect adaptation or an upstream replacement preserves both SQL-backed connection ownership and the required Oracle routing behavior.


## v1.1.0 Duplicate Fold Adaptation

The SQL store's new source-fold helpers use PostgreSQL arrays. Oracle stores
those relationships in `observation_sources`, so both helpers decline the
optimization before reading or updating array columns. The caller retains its
normal semantic rewrite/insert path. For merged-away evidence, a declined fold
requeues the live sources on the same caller connection, preserving coverage and
transaction rollback. Keep the existing ordered Oracle source fences.

From `hindsight-api-slim`, run:

```bash
heavy-slot --label hindsight-v110-fold-routing -- uv run --frozen --extra all pytest -n 1 tests/test_consolidation_fold_fallback.py tests/test_consolidation_scope_parallelism.py tests/test_consolidation_deadlock_order.py tests/test_consolidation_dedup.py
```

The single xdist worker owns a uniquely named disposable pg0 instance on a free
port and drops it at teardown. The fold matrix covers no array SQL on Oracle, PostgreSQL folding, and requeueing
on the caller's connection. This is dialect-routing evidence, not a live Oracle
qualification. Retire the bypass when the store implements an Oracle-native fold
that preserves the same sources, fences and transaction.

## v1.2.0 Fold Refusal And Search-Vector Writes

Store-routed engine folds call `fold_sources_into_observation` or
`fold_observation_into_twin` with the probe-time texts and honor their explicit
boolean refusal. A missing or rewritten target cannot authorize skipping a
CREATE or deleting an UPDATE. The default store implementations refuse missing
or mismatched probe-time rows and propagate a nested fold refusal. This is not
a new atomic-CAS guarantee for every external store: native store overrides
remain responsible for their write-time fences. Oracle still declines the
PG-array optimization separately and keeps the normal rewrite/insert path.

The recovered engine's inline UPDATE/create-fold/update-fold statements must
refresh native `tsvector` and vchord `bm25vector` search columns; direct-text
backends emit no search-vector assignment, and Oracle emits no PG expression.
The engine regression captures SQL at each actual engine apply entry point,
not merely the store's SQL helper. The pg0 matrix runs both fold entries through
inline and store routing, verifies missing/changed-target refusals, preserves
source coverage and temporal widening, and reads back native searchability.
Native PostgreSQL persistence and dialect dispatch proofs do not constitute
live vchord or Oracle qualification.
