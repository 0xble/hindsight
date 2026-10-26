---
sidebar_position: 4
---

# Private Reads

Private recall and reflect use the normal read handlers, request models, authentication, response models, and caller-supplied tag filters. They suppress Hindsight-owned persistent diagnostics for that request without changing global or bank audit and tracing settings.

## Capability And Transport

Send a bodyless `GET /version` and require `features.private_reads` to be `true`. Then use:

```text
POST /v1/default/banks/{bank_id}/private/recall
POST /v1/default/banks/{bank_id}/private/reflect
```

The JSON bodies are the same as [recall](./recall) and [reflect](./reflect). Send authentication exactly as for a public read. Successful private responses include:

```http
X-Hindsight-Private-Read: 1
Cache-Control: no-store
```

A client requiring private reads must reject a missing capability or acknowledgement. It must not retry through the public routes. The acknowledgement is present only on successful responses, not validation, authentication, or provider failures.

## Request Boundary

No-store starts in pure ASGI middleware before body parsing, validation, alias resolution, or authentication. Nested reflect recalls, inherited tasks, and the existing retrieval thread executors carry the same task-local policy. Cancellation restores the caller's context without turning off unrelated public diagnostics. An already-running private worker retains its policy after the requesting task is cancelled.

Private policy short-circuits audit eligibility and bank audit resolution before capture, serialization, scheduling, or database writes. It also short-circuits the composite and database LLM recorders, pending diagnostic writes, Hindsight OpenTelemetry producers and trace-context propagation. Standard logging is blocked at its producer before a log record can reach formatters or queue handlers. OpenTelemetry's outer FastAPI instrumentation excludes the private URLs even when operator URL exclusions are overridden.

Successful results still contain the requested facts or answer. Requested response traces are response data, not permission to persist a diagnostic copy. Unexpected private handler failures return a generic error without formatting a query-bearing traceback for the server logger. Existing authentication and validation errors retain their normal status codes.

## Unsupported Components

Private reads require an initialized engine. Source-owned default and API-key authentication, native configuration resolution, supported native retrieval implementations, and supported in-process LLM providers are eligible. The implementation checks exact component types rather than trusting a subclass or an extension's self-declared flag.

A deployment with an HTTP extension or an operation validator rejects private reads instead of bypassing its hooks. Custom tenant, configuration, memory-store, graph-retrieval, query-analyzer, embedding, and reranking implementations also fail closed before receiving private input. Public routes keep their existing extension hooks and tag restrictions.

LLM CLI/subprocess providers, OAuth wrappers, callback routers, and unsupported provider implementations are rejected. Eligible LLM implementations are the native Anthropic, Fireworks, Gemini, mock, disabled, OpenAI-compatible, and OpenAI Responses implementations. Explicit provider prompt-cache creation is skipped for private calls. Process executors are not eligible because they cannot inherit the request's ContextVars.

## Limits

This is a Hindsight-owned diagnostic no-store boundary, not a database read-only transaction or a remote-provider retention policy. It does not control provider retention, database-server query logging, reverse proxies, external access logs, or instrumentation installed outside these source-owned boundaries. It does not erase diagnostics written by a previous public request.

Absence from an audit log is negative persistence evidence, not proof that retrieval succeeded. Verify a private read through its successful response and returned known evidence, without writing that private response to another diagnostic sink.
