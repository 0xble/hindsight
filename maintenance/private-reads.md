# Private Reads

Part of the root [maintenance contract](../MAINTENANCE.md).

## Preserve

The Hermes incognito consumer needs recall and reflection without persisting their
queries or results to Hindsight diagnostics. Keep the existing public handlers,
authentication, operation validation, tag restrictions, and response models behind
`POST /v1/default/banks/{bank_id}/private/recall` and
`POST /v1/default/banks/{bank_id}/private/reflect`.

A bodyless `GET /version` explicitly advertises `features.private_reads=true`.
The schema default is `false`, so a missing capability never implies support.
Successful private reads acknowledge `X-Hindsight-Private-Read: 1` and
`Cache-Control: no-store`. Consumers fail closed without a public-read fallback.
Preserve the generated OpenAPI and published SDK operations alongside the routes.

The policy is request-local, not a global logging or tracing switch. Propagate it
through native asynchronous tasks and in-process executor dispatch, including
cancellation. Reject opaque stores, validators, provider aliases and process
executors before they receive a private payload when their side effects cannot be
contained. Suppress audit writes, LLM trace writes, request/query log production,
and tracing/export before serialization or scheduling. A private request must not
modify a recording span inherited from a public caller. Public requests retain
their existing diagnostics before and after a private request. Match only the exact
private operation with a nonempty, single-segment bank ID. A mount prefix must not
consume an earlier bank API namespace. Public document IDs ending in a private
operation, including a nested API-looking path, retain their audit, error logging,
tracing and normal response headers. Disconnect monitoring follows the operation
route, not an arbitrary document suffix.

## Verify

The repository-owned `./bin/ci gate <exact-40-hex-HEAD-SHA>` registers the private
API, engine and sink regressions and the published-client real-server story.
From `hindsight-api-slim`, the focused regression command is:

```bash
uv run --frozen --extra all pytest -n 0 tests/test_private_reads.py tests/test_private_reads_engine.py tests/test_private_reads_sinks.py
```

From `hindsight-system-tests`, drive the real server, PostgreSQL, workers and
scripted providers through the published Python client:

```bash
uv run --frozen pytest tests/test_98_private_reads.py
```

The v1.1.0 engine harness stubs `_bank_freshness`, the released internal read
used after reflect authenticates, rather than the public `get_bank_freshness`
method. Its diagnostic connection supports released `executemany` trace writes.
This preserves native authentication, reflection, retrieval and all public/private
sink assertions. The stale-double explanation is falsified by an acquire failure
or missing positive public trace with these released boundaries supplied. No
private-read implementation or diagnostic policy changes in this adaptation.

Keep complete public/private payload comparisons, no-store acknowledgements,
positive public trace/audit controls, complete diagnostic enumeration and zero
additional private trace/audit rows. The real-client story also creates, updates
and deletes documents with private-looking suffixes and observes their persisted
public audit records. Do not replace real client calls with direct
engine access, raw SQL, disabled workers or invented provider responses. Respect
the owning system-test instance and never reuse the live service database.

## Adopt Or Retire

When adopting a released upstream equivalent, check every sink and dispatch
boundary above, capability negotiation and generated SDK compatibility. Retire
the fork implementation only when that release passes the same focused and
real-server contracts. Ordinary public read behavior must remain unchanged.

## Rollback

Revert the source revision and its generated clients together. This patch adds no
database schema or persistent private-read store. Keep consumers fail closed when
an older server no longer advertises support. Source rollback does not erase data
that an unrelated public request previously retained, and does not authorize a
live service restart or global diagnostic reconfiguration.
