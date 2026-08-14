---
title: "20,000 Stars: How Hindsight Got Here, Version by Version"
authors: [benfrank241]
slug: "2026/08/14/hindsight-20k-stars"
date: 2026-08-14T12:00
tags: [hindsight, agent-memory, open-source, milestone, changelog]
description: "Hindsight just crossed 20,000 GitHub stars in under ten months and 67 releases. Here's the timeline: every major capability we shipped, version by version."
image: /img/blog/hindsight-20k-stars.png
hide_table_of_contents: true
---

![Hindsight at 20,000 stars: a release timeline from the first open-source commit in December 2025 to v0.9.1](/img/blog/hindsight-20k-stars.png)

We open-sourced Hindsight in December 2025. Nine months and **67 releases** later, it just crossed **20,000 GitHub stars**. The stars are the side effect. The releases are the story, so here is the actual one: how an agent-memory engine went from a first public commit to what it is today, version by version.

<!-- truncate -->

## The timeline at a glance

| Version | When | Headline |
|---|---|---|
| **0.0.x – 0.1.x** | Dec 2025 | Core retain/recall/reflect on embedded Postgres, local MCP, `hindsight-embed` |
| **0.2.x** | Jan 2026 | Multi-bank memory and cross-bank MCP tools |
| **0.4.x** | Feb – Mar 2026 | Document ingestion, webhooks, Bearer-auth MCP, the integration wave |
| **0.5.x** | Apr 2026 | LinkExpansion graph retriever, delta mental-model refresh, graph view |
| **0.6 – 0.7.x** | May 2026 | AlloyDB ScaNN and ParadeDB BM25 search; Dify, n8n, Google ADK, Flowise |
| **0.8.x** | Jun 2026 | Background maintenance, resumable consolidation, Memory Defense |
| **0.9.0** | Aug 2026 | Knowledge Pages and memory for ten coding agents |
| **0.9.1** | Aug 2026 | ~9x faster extraction, portable banks, xAI OAuth |

## December 2025 — v0.0.x to v0.1.x: the foundation

The first public releases established the core that has not changed since: **retain, recall, and reflect on embedded PostgreSQL**, no external vector database to run. Then the local ergonomics landed fast: a **local MCP server** so any MCP client could connect without a separate service, the **`hindsight-embed`** package to run memory in-process, and an **extensions system** for plugging in new operations. The first graph retriever and a LiteLLM integration shipped before the year was out.

The bet from day one: memory should be a single self-hostable engine, not a stack of services.

## January 2026 — v0.2.x: multi-bank memory

**v0.2.0** added **multi-bank access** and the MCP tools to work across banks, alongside support for Anthropic Claude and LM Studio as providers. Banks are how Hindsight enforces isolation, so this is the release where per-user and per-project memory became real rather than theoretical.

## February to March 2026 — v0.4.x: ingest anything, plug in everywhere

The 0.4 line was the busiest stretch in the project's history, and it pulled in two directions at once.

**Ingest anything.** Hindsight learned to accept **PDFs, images, and Office documents** as inputs, take a **custom extraction prompt**, emit **webhooks** on `consolidation.completed` and `retain.completed`, and authenticate MCP with **Bearer tokens** for real multi-tenant use. Observation scopes and compound tag filtering gave finer control over what memory means and who can see it.

**Plug in everywhere.** The integration wave started here: **Chat SDK, LangGraph, the Vercel AI SDK, the Codex CLI, Claude Code, Agno**, and more, most contributed or co-built with the community.

## April 2026 — v0.5.x: the graph and mental models grow up

**v0.5.0** consolidated the graph story down to a single **LinkExpansion retriever** and added a **co-occurrence graph view** for exploring entity relationships. **Mental models** — standing answers that refresh themselves — got a **delta refresh** that only re-reads memory created since the last run, plus a proper list view. The **OpenAI Agents SDK** integration landed. This is also where the old `hindsight-hermes` pip plugin was retired in favor of the native provider.

## May 2026 — v0.6.x and v0.7.x: scaling the search layer

Two minor versions, one theme: make retrieval fast at real data sizes. **v0.6.0** added **Dify** and **n8n**, **AlloyDB ScaNN** vector indexing, and structured map-type entity labels. **v0.7.0** brought **ParadeDB `pg_search`** as a BM25 backend and configurable tokenization, followed by **Google ADK** and **Flowise** integrations and environment-level control over LLM reasoning effort.

## June 2026 — v0.8.x: built to run in production

The 0.8 line is where Hindsight got operationally serious. **Periodic background maintenance** reconciles consolidation state and enforces retention across tenants on its own. **Durable progress snapshots** made long-running consolidation and batch retains **resumable and inspectable** instead of opaque. On the security side, **Memory Defense** webhook events gained SIEM enrichment, and a flag arrived to **skip storing raw document text** entirely. **OpenHands** and **Zed** joined the integration list.

## August 2026 — v0.9.0: Knowledge Pages, and memory for every coding agent

The headline release. **Knowledge Pages** turned a bank into a self-healing wiki it writes about itself: living documents synthesized from consolidated memory that refresh as the bank learns. And a single plugin brought long-term memory to **ten coding agents** — Claude Code, Codex, Cursor CLI, opencode, Copilot CLI, and more — with per-bank toggles to tune temporal search, graph expansion, and reranking during recall.

## August 2026 — v0.9.1: sharper memory, portable banks

This week's release: roughly **9x faster temporal extraction** with the same results, **whole-bank transfers that carry the Knowledge Pages tree**, async document export, **xAI OAuth** so you can run the LLM lanes on a SuperGrok subscription, and per-bank store capabilities including read-only banks.

## The throughline

Read the timeline back and the pattern is clear. Nothing here is a prompt trick. It is databases, retrieval strategies, consolidation, ingestion, auth, and operational plumbing: the unglamorous infrastructure that makes memory accurate enough to measure and boring enough to run in production. That is the idea 20,000 developers have now starred.

We should stay honest: Hindsight is not the most-starred project in the category, and the benchmarks are where we would rather compete anyway. If you have not tried it, start free on [Hindsight Cloud](https://ui.hindsight.vectorize.io/signup) or self-host in one command from [GitHub](https://github.com/vectorize-io/hindsight). Thank you to everyone who shipped a release, filed an issue, or built something on top. The next version is already in progress.

---

**Learn more:**
- [Full changelog](https://hindsight.vectorize.io/changelog) — every release in detail
- [Hindsight 0.9.0](https://hindsight.vectorize.io/blog/2026/08/06/hindsight-0-9-0) — Knowledge Pages and memory for every coding agent
- [The best open-source agent memory systems](https://hindsight.vectorize.io/blog/2026/08/11/open-source-agent-memory-systems) — an honest look at the whole category
