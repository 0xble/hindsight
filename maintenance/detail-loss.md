# Supported-Detail Preservation

Part of the root [maintenance contract](../MAINTENANCE.md).

## Intentional Divergence

The accepted fork implementation is `ad4f7587931eec0b39607e9a4736acd09b6c63db`.
Preserve its conservative lexical guard when reconciling consolidation refactors.
Brian accepted its lossless identifier-reordering veto on 2026-10-01. Do not
replace it with a more permissive guard without approval of the changed behavior.
This is a lexical acceptance boundary, not a semantic entailment guarantee.

The guard depends on [bounded schema correction](schema-correction.md) for its
shared completion allowance and [generated-language integrity](language-integrity.md)
for independent source-language validation. Its surfaces are
`engine/consolidation/detail_loss.py`, `engine/consolidation/consolidator.py`, and
bounded evidence reads under `engine/memories/`, all in `hindsight-api-slim/hindsight_api/`.

## Adoption And Proof

Keep native exact-source folding and guard fallback insertion distinct: ordinary
CREATE citations follow successfully committed UPDATE survivors, while a rejected
lossy UPDATE retains a separate observation and its own lineage. Preserve the
native store and Oracle restrictions, source/version fences, and
transaction rollback behavior. A failed exact fold must still reach semantic
folding or INSERT rather than losing its source coverage.

The authoritative fork gate registers `test_consolidation_detail_loss*.py` and
`test_consolidation_guard_provenance.py` in `bin/ci`, alongside existing
schema-correction, folding, lane, and language regressions. The public client and
real background-worker story is
`hindsight-system-tests/tests/test_97_guarded_update_provenance.py`. Run database
proofs on isolated named instances, never against a deployed service database.

Upstream disposition: no equivalent was found in the examined upstream tree
`0be6c02b2aafc2b6bdb188ef1842ac507e0cfa2b`; contribution review remains pending.
Fork delivery: the deployed guard is being reconciled with native source-folding
repairs. Landing and runtime activation require separate exact-candidate proof.

Retire only after an adopted upstream equivalent passes the guard and provenance
boundaries without withdrawing Brian's accepted behavior. Rollback reverts the
source integration, never rewrites existing observations or replays committed
consolidations.
