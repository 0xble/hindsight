# Generated-language integrity

Part of the root [maintenance contract](../MAINTENANCE.md).

Keep generated-language enforcement distinct from OCR evidence admission;
use the configurable policy below without destructive changes to source facts.

## HINDSIGHT-005: Generated-language integrity

- **Status:** Active
- **Commits:** `ac28c9a`, `662064e`, `9f9130d`, `01fe0e5`, `e937bd6`, `c695c32`,
  and the source-relative prevention stack `bfc0475`, `90e7f6d`, `01072d5`,
  `276e9a1`, `e91576b`, `7a97026`
- **Surfaces:** generated-language source profiling, retain extraction,
  consolidation, configuration, metrics, multilingual documentation, and focused
  language-integrity tests. The prevention stack adds
  `engine/language_integrity.py` per-output verdicts and code-span exemptions,
  `engine/consolidation/consolidator.py` original-chunk retrieval and pre-write
  dedup checks, `engine/retain/fact_extraction.py` per-dimension validation, and
  the `tests/test_language_prevention*`, `tests/test_language_code_spans.py`, and
  `tests/test_consolidation_language_sources.py` suites.
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
  The source-relative prevention stack replaces aggregate checked/abstained
  counters with per-output `copied`/`preserved`/`mismatch`/`unchecked` verdicts,
  fails closed on detector failure, checks retain dimensions separately from
  deterministic `When:`/`Involving:` labels, and checks consolidation output
  (including final dedup merges against the update anchor and nearest twin)
  against bank-scoped original chunks rather than already-generated fact text,
  before any batch write. Recognizable fenced or inline code and
  source-evidenced quotations stay exempt. `tests/LANGUAGE_PREVENTION.md` holds
  the full contract and the rollout and recovery procedure.
- **Enforcement gate:** prevention is only in effect with
  `HINDSIGHT_API_LLM_LANGUAGE_INTEGRITY=reject` and `HINDSIGHT_API_LLM_OUTPUT_LANGUAGE`
  unset. The shipped default stays `observe`, which records verdicts and accepts
  mismatched output; `retry` corrects once and then accepts. These settings are
  process-scoped, not per bank, so every bank served by a process is in scope.
  Changing the mode on a running service is a separate authorized rollout, not
  part of landing or installing this source.
- **Upstream issue:** [#4016](https://github.com/vectorize-io/hindsight/issues/4016),
  closed as not planned after checked 2026-09-04
- **Upstream PR:** Direct predecessor
  [#4018](https://github.com/vectorize-io/hindsight/pull/4018), closed unmerged after
  maintainer review; this implementation replaces rather than extends that design
- **Regression:** from `hindsight-api-slim`, with an isolated test database,
  `HINDSIGHT_API_DATABASE_URL=pg0://language-prevention-review:55783 HF_HUB_OFFLINE=1`
  `uv run --frozen --extra all pytest -n 0 tests/test_language_integrity.py`
  `tests/test_language_integrity_retain.py tests/test_consolidation_retry_budget.py`
  `tests/test_fact_extraction_retry.py tests/test_language_prevention.py`
  `tests/test_language_prevention_atomicity.py tests/test_language_prevention_dedup.py`
  `tests/test_language_prevention_review.py tests/test_language_code_spans.py`
  `tests/test_consolidation_language_sources.py tests/test_consolidation_batch_atomicity.py`
  `tests/test_consolidation_dedup.py tests/test_worker_retry_knobs.py`. The `all`
  extra is required: the atomicity and dedup suites need the embedded `pg0`.
  `tests/test_consolidation_output_language.py` and
  `tests/test_retain_reflect_output_language.py` use live-provider fixtures and
  stay out of this offline run.
- **Rollback:** Set `HINDSIGHT_API_LLM_LANGUAGE_INTEGRITY=off` immediately, then
  revert the HINDSIGHT-005 patch stack and remove `py3langid` from the lockfile.
- **Retire when:** A released upstream build enforces an equivalent configurable,
  non-destructive-by-default language-integrity policy and passes these focused
  regressions.


Run the API-local regression commands from `hindsight-api-slim`.
