# Original File Preservation

Part of the root [maintenance contract](../MAINTENANCE.md).

## Intentional Divergence

Upstream deletion after file conversion uses the process-level `file_delete_after_retain` policy. This fork permits an individual bank to preserve source evidence through the existing hierarchical configuration resolver. Explicit `false` preserves original uploads. A bank-level `null` inherits the tenant/process policy; a tenant-level `null` inherits the process policy. Neither null becomes a resolved policy value, and other banks retain their existing default. The default remains `true` and deletion still occurs after conversion queues retention.

Upstream [#4367](https://github.com/vectorize-io/hindsight/pull/4367) preserves the downstream document's original-file association for store-owned banks. It does not provide bank-scoped preservation policy. No equivalent policy override was found in the 2026-10-01 upstream preflight at `ec39e10900c6a971f1a73cd37402228d5cccaa25`.

- **Coupled surfaces:** `config.py`, `config_resolver.py`, `_handle_file_convert_retain`, `BankTemplateConfig`, generated template contracts and the configuration documentation. Keep boolean validation, null inheritance and export/import roundtrips together.
- **Failure boundary:** The file worker reconstructs the task's tenant/API-key/retry context and requests fresh, fail-closed configuration before conversion or queueing retention. A failed bank/tenant lookup, missing bank row or malformed persisted preservation override must preserve original bytes and fail the task. Validate the policy before ordinary stored-override coercion can discard it. An existing bank with empty/null config still inherits. Ordinary resolver callers retain their best-effort fallback behavior.
- **Regression:** The exact-SHA gate includes `test_file_preservation_policy.py`, `test_hierarchical_config.py` and `test_bank_template_full_roundtrip.py`. The public-client story `hindsight-system-tests/tests/test_11_original_file_policy.py` uploads a file, waits for the real worker and background consolidation, then uses supported export/download APIs to verify original bytes and default deletion.
- **Adopt Or Retire:** Adopt an upstream equivalent only when it passes bank isolation, boolean/null inheritance, fresh failure-safe policy resolution, document association and public-worker preservation regressions. Remove this support unit when the equivalent replaces every covered surface.
- **Deployment:** Source inclusion is not runtime adoption. Before recovery submissions, deploy through the runtime owner, set only the intended bank's preservation policy through the normal API and read it back. A process-global policy change is a separate action.
