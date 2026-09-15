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
  stale terminal details, and generated clients accept both supported detail types.
- **Upstream issue:** None after checked 2026-09-11
- **Upstream PR:** None after checked 2026-09-11
- **Regression:** `uv run --frozen pytest tests/test_operation_status.py`; generated
  client discriminator tests in `hindsight-clients/{python,go}`; and a successful
  `./scripts/generate-openapi.sh && ./scripts/generate-clients.sh` run.
- **Rollback:** Revert the HINDSIGHT-004 commits and restore callers to treating
  all file-conversion failures as non-terminal.
- **Retire when:** A released upstream build exposes an equivalent stable typed
  terminal failure contract for low-quality OCR.


Run API-local pytest commands from `hindsight-api-slim`; run generation from
the repository root.
