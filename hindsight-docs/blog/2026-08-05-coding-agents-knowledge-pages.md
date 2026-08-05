---
title: "One memory for every coding agent"
description: A single plugin gives Claude Code, Codex, Cursor, opencode, Copilot CLI and more a shared long-term memory of your codebase — the decisions that live in git history and past sessions, plus self-maintaining knowledge pages.
authors: [nicoloboschi]
slug: coding-agents-shared-memory
hide_table_of_contents: true
tags: [coding-agents, knowledge-pages]
---

Your coding agent is brilliant at the first 90% of a fix and blind on the last 10% — the part that hinges on a decision that was never written in the code. A rounding rule. A retry allowlist. A tie-break policy someone argued out in a PR eight months ago. That knowledge lives in git history and past conversations, and every fresh session starts without it.

`hindsight-coding-agents` fixes that. One npm package gives **nine coding agents** a shared, long-term memory of your codebase — and a set of **knowledge pages** that future sessions start from.

<!-- truncate -->

{/* TODO: hero — the 🧠 Using Hindsight Memories banner appearing in a coding-agent session. */}

## One package, every agent

Claude Code, Codex CLI, Cursor CLI, opencode, Kilo CLI, Cline CLI, GitHub Copilot CLI, Grok Build, Antigravity CLI, Devin CLI — they all keep memory in different places, in different formats. The plugin gives them one:

```bash
npm install -g @vectorize-io/hindsight-coding-agents
hindsight-coding-agents install all          # every detected agent, wired natively
hindsight-coding-agents install claude-code   # or just one
```

`install` takes an explicit target, so wiring every agent on the machine is never an accident. It merges native hooks and MCP registration into each agent's own config, backs up anything it touches, and `uninstall` removes exactly what it added.

By default every agent shares **one bank per repository** (`coding-agent::{gitProject}`), so a decision Codex learned this morning is in front of Claude Code this afternoon. Linked worktrees resolve to the same bank. You can split per agent, converge several repos onto a shared team bank, or blacklist a sensitive client — all from one `~/.hindsight/coding-agent.json`.

## Nothing to run — memory just accrues

There is no ingest command. Ingestion happens in the background as you work:

- On a **cold repo**, the first session seeds the bank from recent commit messages and a short headless survey of the codebase structure.
- On **every** session, a background pass ingests conversations not yet stored and the next batch of recent commits _with full diffs, newest first_ — so precision builds up across sessions instead of one giant upfront ingest.
- Every finished session is written back as a transcript: the decisions, minus the mechanical tool-call noise.

Ask the agent `hindsight_sync_status` and it tells you where ingestion stands.

## What the agent actually receives

At the start of a task, the plugin makes a **reflect** call and injects the past decision behind what you're about to do — with its exact rule and values — then caches it for the rest of the session. Every turn, the repo's **knowledge pages** are matched locally against your prompt (a lexical index, no server call, milliseconds) and the most relevant sections are injected with provenance. Below a relevance floor, nothing is injected — retrieval the agent didn't ask for tends to derail it.

When memory shapes an answer, the agent shows a visible `🧠 Using Hindsight Memories` header. And it fails safe: a failed reflect degrades to a normal no-memory turn, never a broken session — but it's always recorded to a diagnostics file, so a memory-less run can't quietly pass for a memory run.

## Knowledge pages: the part that doesn't rot

Raw sessions and commits are the source of truth about _what was said_. But an agent doesn't want to re-derive your architecture every morning — it wants the reconciled version. That's what **knowledge pages** are: living documents the bank writes about itself — architecture, conventions, in-flight initiatives — each answering one question and rewriting itself as the project changes.

They're not hand-maintained files, which is the point. A file is where information goes to age: whoever wrote it last wins, contradictions pile up, nothing reconciles them. A knowledge page is a **projection over processed memory** — Hindsight has already extracted the facts, deduplicated them, and reconciled their contradictions through consolidation. When the team decided X and later amended it to Y, the page says Y, and can say why. Delete a page and nothing is lost; it re-projects from memory.

During a session the agent can list, read, and update its own pages, or record a new initiative it's starting. Between sessions, `hindsight fs mount` projects the whole knowledge base onto disk as ordinary markdown — `grep` it, open it in your editor, commit it elsewhere.

## Migrating off the per-agent plugins

If you used the older single-agent integrations (`hindsight-claude-code`, `hindsight-cursor-cli`, `hindsight-codex`, …), this package supersedes them. Old banks were scoped per agent per project and can't be merged server-side — so instead of moving data, re-import the conversations the agent already wrote to disk:

```bash
cd /path/to/your/repo
hindsight-coding-agents install claude-code --import-conversations
```

Imports are attributed only when the session itself recorded the directory it ran in — never guessed from a folder name, because a wrong guess files someone else's conversation into your bank. The import is scoped to the current repo and safe to re-run.

## Try it

Point the plugin at a local Hindsight and install it into your agent of choice:

```bash
docker run -d -p 8888:8888 -p 9999:9999 -e HINDSIGHT_API_LLM_PROVIDER=gemini \
  -e HINDSIGHT_API_LLM_API_KEY=$GEMINI_API_KEY -e HINDSIGHT_API_LLM_MODEL=gemini-2.5-flash \
  ghcr.io/vectorize-io/hindsight:latest

npm install -g @vectorize-io/hindsight-coding-agents
hindsight-coding-agents install all
```

Then just work. The last 10% starts showing up on its own.
