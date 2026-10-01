# OCR evidence admission and terminal failures

Part of the root [maintenance contract](../MAINTENANCE.md).

Reject unusable OCR without discarding useful sparse, multilingual, or partially
uncertain evidence or parser fallback. Admission and typed terminal status must
remain compatible for callers.

## HINDSIGHT-001: OCR evidence admission

- **Status:** Active
- **Commits:** `2fa19ab`, `83d7ad3`, `8c107c6`, `19357a9`, `b684912`, `60128d9`
- **Surfaces:** `engine/parsers/{__init__,ocr_quality}.py`, `tests/test_ocr_quality.py`
- **Upstream issue:** https://github.com/vectorize-io/hindsight/issues/3897
- **Upstream PR:** [#4047](https://github.com/vectorize-io/hindsight/pull/4047),
  closed unmerged: the maintainer preferred explicit MarkItDown flags over heuristics.
  The admission policy remains an intentional fork divergence.
- **Mixed-chain precedence (M1):** Relative to released base
  `5fc4ce20917b916240cef27c212c387a177f115b`, this is fork-owned: OCR rejection
  entered in `996c59b9268f58f3e2059079b563adfa5f467b59`; the `nonempty_error`
  slot in `27fcb7b95013afaf4fb4253927b4dcca390921ba` still let a later OCR
  rejection hide a provider/transport failure. Preserve an unclassified error
  separately, in either parser order, rather than settling a retryable mixed
  chain as `low_quality_ocr`. Unsupported file types are deterministic: they
  must not outrank a capable parser's OCR rejection, even with trailing empty
  results. Keep OCR errors separately from unsupported-type errors; only
  transient/unclassified failures take priority. Exhaust fallback first; a useful
  result still succeeds and low-quality-only or low-quality/unsupported chains
  still have typed OCR details.
  `tests/test_ocr_quality.py` checks the raised error and failure metadata,
  including wrapped errors and a trailing empty parser.
- **Fence/sparse-text provenance:** The optional-newline wrapper bug originated
  in fork commit `f7a140b6ad68e5ab9e56fcc6ef7bddb0c572bd2a`; unconditional
  UI-only rejection originated in `08de007103ba1de80a805301ded41847de8e8f11`.
  Preserve single-line fenced transcriptions. UI rejection requires distinct
  corroborating cues including a control; a lone label or timestamps alone
  are not sufficient evidence of unusable OCR.
- **Regression:** `uv run --frozen pytest tests/test_ocr_quality.py`
- **Rollback:** Revert the listed commits in reverse order and rerun the regression.
- **Retire when:** A released upstream build provides equivalent admission,
  fallback, privacy, sparse-text, and multilingual behavior and passes this test.

## HINDSIGHT-004: Structured OCR terminal failures

- **Status:** Active
- **Commits:** `1fe5fce`, `f405858`, `fd7fca5`
- **Surfaces:** API operation-detail models/status persistence, checked-in OpenAPI
  contracts, generated Python/TypeScript/Go clients, and `scripts/generate-clients.sh`
- **Behavior:** Failed `file_convert_retain` operations expose a stable, discriminated
  `low_quality_ocr` detail with the OCR quality reason, so callers can settle
  deterministic evidence exclusions without parsing error prose. Retries clear
  stale terminal details, and generated clients accept both supported detail types,
  including raw dictionary and JSON Pydantic validation nested in operation responses.
- **Nullable discriminator correction (L7):** The Python/Go oneOf wrappers were
  introduced by fork `fded26c852a11c2ffc2b99a106544c4ce3ec3979` after released
  base `5fc4ce20917b916240cef27c212c387a177f115b`; upstream's single refresh
  detail had no such union. Python trial decoding counted JSON null as two
  matches; Go chose a shape even when `operation_type` named another variant.
  Keep `scripts/patch-operation-details-client.py` as the source owner for both
  generated wrappers. Run its language-specific hook after each language's
  generation; null represents no actual instance, unknown/missing discriminators
  and mismatched shapes are rejected, and parents retain absent/explicit-null
  semantics. Reapplying the patch must be byte-idempotent and changed generator
  boundaries must fail without rewriting the target.
- **Empty-detail wire contract:** `engine/memory_engine.py::_operation_details`
  returns a validated, discriminated model dump or `None`, never `{}` or `""`;
  both list/get operation readers use that projection. `api/http.py` response
  models and checked-in OpenAPI permit only the typed union or null. Malformed,
  missing, legacy, or unreported metadata resolves to null. Preserve strict
  Go/Python rejection of empty non-null details rather than accepting a wire
  shape the server cannot emit. `tests/test_operation_status.py` guards the
  projection and server-model boundary; client null/discriminator and generator
  idempotence tests remain required.
- **Qualification preflight:** Read upstream `AGENTS.md`, `CONTRIBUTING.md`,
  `CLAUDE.md` and code-review guidance at pinned
  `d863f78aa24408583d69bbc32203649fc6fc230a`. Independent proposal: retain
  non-terminal parser failures separately and decode nullable unions once by
  `operation_type` through the existing generation patch. Related upstream
  [#4047](https://github.com/vectorize-io/hindsight/pull/4047) remains closed
  unmerged (explicit MarkItDown flags preferred); merged
  [#3609](https://github.com/vectorize-io/hindsight/pull/3609) establishes null
  details for older/in-flight/unreporting operations, but not the fork's OCR
  union. Neither replaces these corrections. Both findings are fixed as retained
  fork divergences; no upstream write or new contribution is part of this work.
- **Generation proof:** OpenAPI Generator 7.10.0 was run for Python and Go against
  the checked-in spec/config in isolated scratch using Java, then the maintained
  patch and Go value-receiver/formatting postprocessing were applied. Both
  operation-detail wrappers matched the checked-in candidate byte-for-byte;
  two subsequent patch/format passes were unchanged. The full multi-language
  Docker/Rust/TypeScript generation script is not needed for these two wrappers.
  `hindsight-clients/python/tests/test_operation_details.py` also verifies patch
  idempotency and fail-closed behavior for both languages.
- **Oracle adaptation:** Brian Le's retry-clear change
  `c343c30c202466a636f4d4560d09954e3c618362` introduced CASE/chained JSONB merges
  the Oracle adapter cannot translate. Keep native CLOB `JSON_MERGEPATCH` in the
  existing failure/retry methods without changing PostgreSQL merges or atomic status
  guards. Oracle removes null-valued failure keys; PostgreSQL keeps nulls. Both
  suppress stale details without clearing unrelated metadata.
- **Upstream disposition:** Fork-only correction; no contribution opened. Guidance
  checked at `752fcf512d44a47bd0f2876e8c308074da5abb4e` (`AGENTS.md` → `CLAUDE.md`).
  Related [#4628](https://github.com/vectorize-io/hindsight/pull/4628) requires CLOB
  merge results; [#5040](https://github.com/vectorize-io/hindsight/pull/5040) fixes a
  different operation checkpoint. Neither replaces the typed-failure contract.
- **Upstream issue:** None after checked 2026-09-11
- **Upstream PR:** None after checked 2026-09-11
- **Regression:** `uv run --frozen pytest tests/test_operation_status.py tests/test_operation_metadata_sql.py`;
  the latter captures actual engine statements through Oracle rewriting and checks
  PostgreSQL merge/status/rollback behavior. Oracle coverage is translator-only,
  not live-database execution. Generated
  client discriminator tests in `hindsight-clients/{python,go}`; and a successful
  `./scripts/generate-openapi.sh && ./scripts/generate-clients.sh` run.
- **Rollback:** Revert the HINDSIGHT-004 commits and restore callers to treating
  all file-conversion failures as non-terminal.
- **Retire when:** A released upstream build exposes an equivalent stable typed
  terminal failure contract for low-quality OCR.

## HINDSIGHT-006: Typed no-extractable-text failures

- **Status:** Active
- **Commits:** `27fcb7b`
- **Surfaces:** `engine/parsers/{__init__,base,markitdown,iris,llama_parse}.py`, `engine/memory_engine.py`,
  `engine/operation_details.py`, checked-in OpenAPI contracts, generated
  Python/TypeScript/Go clients, and `tests/test_no_extractable_text.py`
- **Behavior:** When every parser in the chain returns empty content, the failed
  `file_convert_retain` operation exposes `failure_class=no_extractable_text`,
  `failure_reason=empty_content`, and the ordered `parsers` chain it tried. Mixed
  chains and transient errors stay unclassified. Callers can settle image-only PDFs
  without resubmitting them, and re-probe when the parser chain changes.
- **Adapter integration:** Empty-success `RuntimeError` paths originated upstream
  in Iris `7eafba661e15fa1f6c35f827099b215dc10fbfbe` and LlamaParse
  `91106f30ef8eb2e192664acf33dad986569bd731`, before the released base
  `5fc4ce20917b916240cef27c212c387a177f115b`. The fork typed-chain contract
  introduced by `27fcb7b95013afaf4fb4253927b4dcca390921ba` requires adapting
  successful null/empty/whitespace results to `NoExtractableContentError`.
  Keep provider, transport, and job failures unclassified; do not infer no-text
  from arbitrary exception messages. Preserve unclassified failures separately
  from OCR rejection so either ordering of a mixed chain remains unclassified.
- **Upstream issue:** https://github.com/vectorize-io/hindsight/issues/3255 (scanned
  PDFs; the typed failure is fork-only)
- **Upstream PR:** None. Upstream closed PDF OCR in #3442 pending a better parser.
- **Regression:** `uv run --frozen pytest tests/test_no_extractable_text.py tests/test_operation_status.py`
  `tests/test_iris_parser_stub.py tests/test_llama_parse_parser.py tests/test_parser_empty_output.py`
  and the generated client discriminator tests in `hindsight-clients/{python,go}`.
- **Rollback:** Revert the listed commit; callers fall back to treating the failure
  as transient.
- **Retire when:** A released upstream build extracts image-only PDFs or exposes an
  equivalent typed empty-content failure.

The terminal `failure_reason` remains a scalar string on the wire. Its OpenAPI
schema flattens OCR reasons and `empty_content` into one string enum while the
API retains the typed union and failure-class validation. An `anyOf(enum,
const)` emitted a generated Python wrapper that rejected raw nested reason
strings. The generated operation-detail regression checks raw dictionary/JSON
validation and preserves the reason value through serialization.


Run API-local pytest commands from `hindsight-api-slim`; run generation from
the repository root.
