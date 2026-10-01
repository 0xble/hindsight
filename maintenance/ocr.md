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
- **Upstream PR:** None after checked 2026-09-11
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
- **Upstream issue:** None after checked 2026-09-11
- **Upstream PR:** None after checked 2026-09-11
- **Regression:** `uv run --frozen pytest tests/test_operation_status.py`; generated
  client discriminator tests in `hindsight-clients/{python,go}`; and a successful
  `./scripts/generate-openapi.sh && ./scripts/generate-clients.sh` run.
- **Rollback:** Revert the HINDSIGHT-004 commits and restore callers to treating
  all file-conversion failures as non-terminal.
- **Retire when:** A released upstream build exposes an equivalent stable typed
  terminal failure contract for low-quality OCR.

## HINDSIGHT-006: Typed no-extractable-text failures

- **Status:** Active
- **Commits:** `27fcb7b`
- **Surfaces:** `engine/parsers/{__init__,base,markitdown}.py`, `engine/memory_engine.py`,
  `engine/operation_details.py`, checked-in OpenAPI contracts, generated
  Python/TypeScript/Go clients, and `tests/test_no_extractable_text.py`
- **Behavior:** When every parser in the chain returns empty content, the failed
  `file_convert_retain` operation exposes `failure_class=no_extractable_text`,
  `failure_reason=empty_content`, and the ordered `parsers` chain it tried. Mixed
  chains and transient errors stay unclassified. Callers can settle image-only PDFs
  without resubmitting them, and re-probe when the parser chain changes.
- **Upstream issue:** https://github.com/vectorize-io/hindsight/issues/3255 (scanned
  PDFs; the typed failure is fork-only)
- **Upstream PR:** None. Upstream closed PDF OCR in #3442 pending a better parser.
- **Regression:** `uv run --frozen pytest tests/test_no_extractable_text.py tests/test_operation_status.py`
  and the generated client discriminator tests in `hindsight-clients/{python,go}`.
- **Rollback:** Revert the listed commit; callers fall back to treating the failure
  as transient.
- **Retire when:** A released upstream build extracts image-only PDFs or exposes an
  equivalent typed empty-content failure.


## HINDSIGHT-009: Bounded Scanned PDF OCR

- **Status:** Maintained fork divergence, 2026-10-01.
- **Surfaces:** `engine/parsers/{markitdown,pdf_ocr}.py`,
  `tests/{test_pdf_ocr,pdf_ocr_fixtures}.py`,
  `hindsight-system-tests/tests/test_91_pdf_ocr.py`, and explicit package pins for
  the existing `pypdfium2==5.4.0` and added `psutil==7.2.2`.
- **Behavior:** With existing MarkItDown OCR enabled, PDF pages lacking extracted
  text render sequentially and use the same configured image OCR endpoint,
  model, headers and prompt. Text-only conversion preserves the existing
  whole-document converter. Mixed documents preserve text and scanned-page order.
  Every OCR page passes existing evidence admission before any result returns.
  A failed, refused or unusable page rejects the whole conversion. Invalid or
  encrypted PDFs do not become scans. Disabled OCR invokes no provider.
- **Bounds:** Scanned and mixed PDFs have a 32 MiB input, 20-page, 10-million-pixel page,
  50-million-pixel rendered total, 16 MiB rendered page, 64 MiB rendered total,
  and 1 MiB extracted-output ceiling, applied after native text-layer
  classification and before OCR. Text-only PDFs use the original whole-document
  converter without these input, page or output caps. Rendering uses scale 2. All
  OCR-enabled PDF conversions, including text-only classification/extraction,
  remain process-isolated with CPU, scratch-write and sampled RSS protection. The document
  deadline is 120 seconds, individual requests at most 30 seconds with SDK retries
  disabled. Parent polling every 50 ms samples a 1 GiB RSS ceiling. This is sampled
  protection, with possible between-sample overshoot, not an OS hard memory cap.
  Unavailable RSS measurement fails closed. Both Linux and Darwin use the sampled
  RSS watchdog, with no virtual-address-space limit. POSIX CPU (60 seconds) and
  scratch-write (64 MiB per file, including the result transport) limits apply
  where available. Darwin is the exercised platform, Linux is not validated here.
- **Isolation:** A disposable spawned process owns PDFium, whose documented
  thread-safety contract forbids concurrent PDFium calls across threads. Parent
  termination, kill and join on timeout, cancellation or RSS rejection precede
  cleanup of protected scratch. No live client object is pickled. Already
  configured OCR settings travel only through multiprocessing's in-memory
  bootstrap pipe, never argv or scratch files. Child error transport includes
  type names or non-content admission measurements, not provider error text.
- **Preservation:** No upload, task payload, storage key or original bytes are
  replaced. Existing file-conversion handling forwards source metadata and date
  to a separate retain operation. The system story explicitly uses
  `HINDSIGHT_API_FILE_DELETE_AFTER_RETAIN=false`. The shipped default remains true
  and still deletes storage after queuing downstream retain, before its success
  is proven. Source preservation is a separate runtime-owner prerequisite before
  any recovery retry. Protected backup copies alone do not prove the original
  database file association survives. This patch adds no per-file preservation
  override and changes no live profile, worker, provider, model or routing.
- **Upstream:** Preflight pinned `ec39e10900c6a971f1a73cd37402228d5cccaa25`.
  [Issue #3255](https://github.com/vectorize-io/hindsight/issues/3255) is open.
  [PR #3442](https://github.com/vectorize-io/hindsight/pull/3442) is closed,
  unmerged, with dissent preferring a more complete separate provider. This fork
  intentionally keeps the existing provider route and rejects partial-page
  success. It does not claim upstream acceptance. MarkItDown OCR plugin changes
  are not part of this patch.
- **Regression:** `uv run --frozen pytest -n 0 tests/test_pdf_ocr.py
  tests/test_markitdown_parser.py tests/test_ocr_quality.py
  tests/test_no_extractable_text.py`. Run the blackbox story from
  `hindsight-system-tests` with its isolated pg0 database. Tests use real PDF,
  HTTP and process mechanics with synthetic provider output. They do not prove
  real-model OCR fidelity. Both scan and valid encrypted fixtures are generated
  transparently at runtime, with no added binary files or generator dependency.
  Text compatibility regressions use actual 21-page and greater-than-32-MiB PDFs,
  and a transparent Unicode font mapping producing greater-than-1-MiB extracted
  text. Their full outputs must match the OCR-disabled original converter with
  zero provider calls. Mixed-PDF input/page/output rejection remains covered.
  The successful public-API story lets ordinary background consolidation run,
  verifies its completed operation and observation evidence link, and preserves
  the original uploaded bytes. No observation/consolidation enablement is disabled
  to make the story deterministic.
- **Rollback:** Revert this extension and its dependency pins. Existing raster
  OCR, parser fallback and no-extractable-text behavior remain available.
- **Retire when:** A released upstream parser provides equivalent bounded,
  cancellation-safe scanned-page extraction, per-page admission, atomic failure
  and original-file provenance through the same configured route.

Run API-local pytest commands from `hindsight-api-slim`; run generation from
the repository root.
