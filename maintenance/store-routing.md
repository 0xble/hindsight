# SQL-Store Routing on Oracle

Part of the root [maintenance contract](../MAINTENANCE.md).

## HINDSIGHT-010: SQL-backed consolidation reads

- **Status:** Active
- **Origin:** Fork-only regression introduced by the Oracle table-routing adaptation in `faf3fa46220ffb5aeb24b3df1e3d35171f68bbd1`. The reconciled upstream store boundary used `store.store_owned_for(bank_id)` for the default SQL store; widening that predicate to Oracle made `_resolve_original_source_texts()` pass `conn=None` to the SQL-backed PostgresMemories implementation.
- **Surfaces:** `hindsight-api-slim/hindsight_api/engine/consolidation/consolidator.py` and its focused consolidation regression.
- **Behavior:** Route memory-row and chunk-body reads through the store API only for store-owned banks. SQL-backed stores, including Oracle-routed SQL fixtures, acquire and pass a real connection to `get_memories()` and read chunk bodies through the SQL connection. Keep Oracle table-dialect routing for the SQL statements that require it.
- **Upstream disposition:** No upstream bug; the failing predicate was fork-only. The upstream store boundary at the reconciled checkpoint uses the store-ownership predicate for this source-read path. No upstream issue or PR applies.
- **Guidance and proposal:** Preserve the existing store owner as the source-of-truth boundary, and keep dialect selection separate from storage ownership. Do not make SQL-backed reads connectionless merely because Oracle SQL routing is active.
- **Regression:** From `hindsight-api-slim`, run `uv run --frozen pytest -q tests/test_consolidation_scope_parallelism.py::test_oracle_sql_store_source_read_uses_connection`. The test is red when the Oracle dialect predicate sends `conn=None` to the SQL store and green when a real acquired connection is used.
- **Rollback:** Revert only the source-read predicate correction, its regression, and this record; retain the broader Oracle SQL-dialect routing required by the fork contract.
- **Retire when:** The fork no longer carries the Oracle SQL-dialect adaptation or an upstream replacement preserves both SQL-backed connection ownership and the required Oracle routing behavior.
