# Source-relative language prevention

This candidate does not deploy or change any bank policy. `observe` still accepts
mismatches. The enforceable mode is `llm_language_integrity=reject` with
`llm_output_language` unset. `retry` corrects once but remains fail-open; it is not
prevention. An explicit output-language configuration is the supported deliberate
translation override and bypasses source-language checking.

## Contract

- Retain and consolidation perform one language-correction attempt, then reject
  mismatched **or unchecked** output in reject mode. Detector failures fail closed.
- Per-output text-free verdicts are `copied`, `preserved`, `mismatch`, or `unchecked`,
  including source keys, reason and policy version. Aggregate checked/abstained
  counters do not substitute for these verdicts. Short/ambiguous natural language
  is unchecked, not a validated pass; this can hold legitimate work for review.
- Source-evidenced copied spans and quotations are not translation errors. They
  do not license translating the surrounding account. This is language validity,
  not assertion/fact validation: copying a quotation does not establish its truth.
- Genuine multilingual source prose supports corresponding output-language
  segments. Code and name-sized identifiers are not forced into English.
- Consolidation obtains bank-scoped original chunks for incoming facts and the
  visible source facts of recalled observations. Generated fact text is never a
  fallback language authority. Missing original evidence remains unchecked and
  blocks novel prose in reject mode. Stores that cannot implement original-chunk
  retrieval via `get_chunk_texts` fail closed in strict consolidation instead of
  guessing.
- Original-source language codes accompany instructions so an old translated fact
  is not also the only language cue in the corrective prompt.

## Recovery and rollout (separate authorization required)

1. Independently review and merge a pinned candidate; build a release and run the
   offline fixtures plus isolated PostgreSQL tests below.
2. Preserve the existing runtime, effective bank config and queue/operation state
   for rollback. Deploy/restart only with explicit approval.
3. Enable reject on the intended personal bank only; leave output language unset.
   Read back effective config in API and worker processes. Do not claim prevention
   while observe/retry or an overriding output language remains effective.
4. Monitor per-output unchecked/mismatch/copied counters and failed operations.
   Missing chunks, attachments without sufficient source text, or low-confidence
   prose can reject legitimate records. Review these rather than silently retrying
   with language checks disabled. Verbatim ingestion is a source-preserving path.
5. Consolidation rejection happens before its write transaction: existing
   observations, original documents/chunks and pending source facts remain intact.
   A rejected synchronous retain leaves its caller responsible for retaining and
   resubmitting the original input. Async operation input is the recovery source;
   do not discard failed operations. Multi-chunk retain has existing streaming
   transaction boundaries, not a new whole-document atomicity guarantee.
6. Rollback means reverting release/policy after separate approval, not deleting
   source data or marking rejected work successful. Existing-record cleanup,
   invalidation, retries and curation are separately authorized work.

## Deterministic tests (no provider generation)

Run from `hindsight-api-slim`, in an isolated test database (never production):

```sh
HINDSIGHT_API_DATABASE_URL=pg0://language-prevention-test:55679 HF_HUB_OFFLINE=1 \
uv run pytest -n 0 tests/test_language_prevention.py \
  tests/test_language_prevention_atomicity.py tests/test_language_integrity.py \
  tests/test_language_integrity_retain.py tests/test_consolidation_retry_budget.py \
  tests/test_consolidation_language_sources.py tests/test_consolidation_batch_atomicity.py
```

`test_consolidation_output_language.py` and `test_retain_reflect_output_language.py`
use live-provider fixtures and must not be included in an offline/no-probe run.
The unit tests cover the configured-translation bypass without a provider.
Private historical records must stay outside this repository; replay their actual
fact/observation text against their saved original chunks, including the quoted
French span and English-to-Spanish/German/Chinese drift.
