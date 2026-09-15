# Generated-language integrity

Part of the root [maintenance contract](../MAINTENANCE.md).

Keep generated-language enforcement distinct from OCR evidence admission;
use the configurable policy below without destructive changes to source facts.

## HINDSIGHT-005: Generated-language integrity

- **Status:** Active
- **Commits:** `ac28c9a`, `662064e`, `9f9130d`, `01fe0e5`, `e937bd6`, `c695c32`
- **Surfaces:** generated-language source profiling, retain extraction,
  consolidation, configuration, metrics, multilingual documentation, and focused
  language-integrity tests
- **Behavior:** Conservatively profile source language once outside async hot paths,
  detect confident generated drift with maintained `py3langid`, and emit bounded
  metrics in the default `observe` mode. Operators may select `retry` to add generic
  source-language guidance, regenerate once, and then preserve availability by
  accepting a persistent mismatch. Explicit fail-closed `reject` leaves source facts
  unmodified and eligible for an operator-controlled retry. `off` disables the guard.
  Retain Batch API remains available in `off` and `observe`; `retry` and `reject`
  route through the live provider path so enforcement cannot be bypassed by batch results.
  The guard abstains on short, ambiguous, materially multilingual, and unsupported
  same-script inputs. Before aggregate generated-language abstention, a dependency-free,
  source-relative script check catches substantial unquoted non-Latin prose absent from
  a source with minimum Latin evidence while preserving copied quotations, literal code,
  short names, and legitimate non-English sources. Source code literals remain
  available as evidence when a generated fact restates their values in prose.
  Bounded source-backed affix matching preserves word-script name inflections
  without raising the prose threshold or applying stem matching to CJK.
- **Upstream issue:** [#4016](https://github.com/vectorize-io/hindsight/issues/4016),
  closed as not planned after checked 2026-09-04
- **Upstream PR:** Direct predecessor
  [#4018](https://github.com/vectorize-io/hindsight/pull/4018), closed unmerged after
  maintainer review; this implementation replaces rather than extends that design
- **Regression:** `uv run --frozen pytest tests/test_language_integrity.py tests/test_language_integrity_retain.py tests/test_consolidation_retry_budget.py tests/test_fact_extraction_retry.py`
- **Rollback:** Set `HINDSIGHT_API_LLM_LANGUAGE_INTEGRITY=off` immediately, then
  revert the HINDSIGHT-005 patch stack and remove `py3langid` from the lockfile.
- **Retire when:** A released upstream build enforces an equivalent configurable,
  non-destructive-by-default language-integrity policy and passes these focused
  regressions.


Run the API-local regression commands from `hindsight-api-slim`.
