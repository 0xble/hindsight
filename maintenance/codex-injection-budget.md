# Codex Injection Budget

Part of the root [maintenance contract](../MAINTENANCE.md).

## Intentional Divergence

The Codex integration at fork base `a82a38a0e93968d53ce61e5ceda77252318fc3f8`
passed the token cap to recall but appended unbudgeted context. This retained
patch owns the complete injection cap in `hindsight-integrations/codex/scripts/`.
Upstream [#3122](https://github.com/vectorize-io/hindsight/issues/3122) concerns
reflect serialization and does not replace this hook boundary. No equivalent
Codex patch was found during the 2026-10-01 upstream preflight. Fork publication follows the protected gate route and is separate from installed
runtime adoption.

At historical fork base `be6bddb66ed4d3a9676f82b96a7422e683dae1f3`, the active Python
prompt hook submitted raw injected-memory and transport wrappers as recall
queries. Its existing content filter now mirrors the coding-agents structural
tag rule for both automatic queries and retained text. Wholly synthetic prompts
stop before bank mission, API or state work. Real surrounding text and prior
human context remain queryable. This consumer recalls each prompt, with no
automatic reflection or bounded session-injection retry budget to consume.
Known opening tag names must end with whitespace or `>`, so a hyphen or colon
suffix is preserved even when followed by a known closing tag. An unclosed
wrapper is conservatively preserved. The hook and content regressions prove
synthetic admission, clean multi-turn queries, actual retained API output in
both text and JSON modes, and preservation of unknown literal tags. Keep this boundary when
adopting source through `hindsight-docs/static/get-codex`. That installer also
rewrites host wiring, so a runtime-only repair must preserve the existing hooks,
configuration and authentication rather than rerun its broad installation.

- **Coupled surfaces:** `recall.py`, `recall_launcher.py`,
  `lib/recall_context.py`, `lib/token_budget.py`, hook template and
  `hindsight-docs/static/get-codex`. Preserve whole-fact packing and accurate
  emitted counts when adopting upstream changes to any of these surfaces.
- **External drift:** The optional tokenizer is pinned to `tiktoken==0.12.0`.
  Its o200k regex and asset hash must change together. Hook execution must not
  fetch a missing asset. Installer reruns rebuild only the private, unredirected
  tokenizer directory, and launcher startup failure selects current Python
  before recall starts. Never retry an already started hook.
- **Regression:** Run the integration suite with `uv run --no-project --with pytest --with tiktoken==0.12.0 pytest hindsight-integrations/codex/tests -q`.
  Set `CODEX_TEST_O200K_ASSET` to the installer-provisioned asset to require the
  exact-tokenizer cases, otherwise they skip. `test_recall_launcher.py` proves
  stale-interpreter recovery through real subprocesses. Also check the installer
  with `bash -n hindsight-docs/static/get-codex`.
- **Adopt Or Retire:** Replace this unit only when an upstream equivalent passes
  the complete rendering, whole-fact, emitted-count and offline recovery
  regressions. A deliberate migration to another coding-agent integration also
  permits retirement after its installed injection cap is demonstrated. Rollback removes this hook-specific divergence and its private tokenizer setup,
  leaving server recall behavior unchanged.
