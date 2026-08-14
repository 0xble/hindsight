---
title: "Hindsight Hits 20,000 Stars: By the Numbers"
authors: [benfrank241]
slug: "2026/08/14/hindsight-20k-stars"
date: 2026-08-14T12:00
tags: [hindsight, agent-memory, open-source, milestone, community]
description: "Hindsight just crossed 20,000 GitHub stars in under ten months. Here's the milestone by the numbers: releases, integrations, contributors, and the benchmarks behind them."
image: /img/blog/hindsight-20k-stars.png
hide_table_of_contents: true
---

![Hindsight at 20,000 stars, by the numbers: 20K stars, 67 releases, 60+ integrations, and benchmark-leading recall, all in under ten months](/img/blog/hindsight-20k-stars.png)

We open-sourced Hindsight in late October 2025. Nine and a half months later, it just crossed **20,000 GitHub stars**. That is roughly 69 new stars a day, every day, since the first commit went public.

A star count is a vanity metric on its own. What makes this one worth writing down is what sits behind it: a specific set of design bets about how agent memory should work, and a lot of shipping. Here is the milestone by the numbers.

<!-- truncate -->

## The headline

- **20,000 stars** in under ten months (created October 30, 2025).
- **1,400+ forks**.
- **~190 contributors**.
- **2,500+ commits** on `main`.

We should be honest about one thing up front: Hindsight is not the most-starred project in the agent-memory category. Mem0, Cognee, and Graphiti all have larger communities, and that is worth acknowledging. What 20,000 stars tells us is narrower and, we think, more useful: a particular thesis about memory is resonating with the engineers who have to run it in production.

## Shipping: 67 releases and counting

Memory systems live or die on iteration speed, so we ship constantly.

- **67 releases** since `v0.0.16` in December 2025, an average of about **1.6 releases a week**.
- The latest, **v0.9.1**, landed this week: roughly **9x faster** temporal extraction, portable bank transfers that carry Knowledge Pages, and xAI OAuth support.
- Two releases back, **0.9.0** shipped **Knowledge Pages** (a self-healing wiki your memory bank writes about itself) and a single plugin that brings memory to **ten coding agents**.

The cadence is the point. A memory engine that improves every few days compounds: the recall your agent gets in August is measurably better than the one it got in May.

## The ecosystem: 60+ integrations

Memory is only useful if it plugs into where your agents already run.

- **60+ integrations and clients**, from frameworks (LangGraph, CrewAI, the Vercel AI SDK) to chat platforms and voice.
- **10 coding agents** get long-term memory from one package: Claude Code, Codex, Cursor CLI, opencode, GitHub Copilot CLI, Cline, Kilo, Grok Build, Antigravity, and Prime Agent.
- Hindsight is also a portable **Agent Plugin**, so any client on the new Vercel/OpenAI/AWS/Cursor/GitHub standard can load it.

## The numbers that actually matter: accuracy

Stars measure attention. Benchmarks measure whether the memory works. This is where we spend most of our effort.

- **94.6%** on LongMemEval-s, near the top of the leaderboard.
- **#1 at the 10-million-token tier** of BEAM, the long-context memory benchmark, at 64.1%.
- On a 61-task coding benchmark, giving Claude Code memory cut the corrections it needed by about **57%**, at lower cost and faster wall time.

Underneath those results is the design thesis the stars are really voting for: **embedded PostgreSQL, no external vector database**, four retrieval strategies (semantic, keyword, graph, temporal) with cross-encoder reranking, and a consolidation step that reconciles contradictions instead of piling up duplicates. One Docker command, MIT licensed.

## The community: 130+ posts and a shelf of builder stories

A lot of the best material about Hindsight was not written by us.

- **130+ posts** on this blog, from feature deep-dives to honest competitor comparisons.
- **Guest posts from community builders** who shipped real products on Hindsight: an AI note-taking extension, a Claude Code agent with long-term memory, a multi-user financial assistant, and more.

Those stories matter more than any number here, because they are proof that the memory layer holds up outside our own benchmarks.

## What the 20,000 is really counting

Every one of these numbers points at the same bet: that agent memory is an infrastructure problem, not a prompt trick. That it should be self-hostable in one command, accurate enough to measure, and boring enough to run in production without a second database to babysit. Twenty thousand developers have now starred that idea.

Thank you to everyone who filed an issue, opened a PR, wrote a guest post, or just tried it in a weekend project. The next milestone we care about is not the star count. It is the corrections your agent no longer has to make.

If you have not tried it yet, you can start free on [Hindsight Cloud](https://ui.hindsight.vectorize.io/signup), or self-host it in one command from [GitHub](https://github.com/vectorize-io/hindsight).

---

**Learn more:**
- [Hindsight on GitHub](https://github.com/vectorize-io/hindsight) — star it, fork it, or read the code
- [Hindsight 0.9.0](https://hindsight.vectorize.io/blog/2026/08/06/hindsight-0-9-0) — Knowledge Pages and memory for every coding agent
- [The best open-source agent memory systems](https://hindsight.vectorize.io/blog/2026/08/11/open-source-agent-memory-systems) — an honest look at the whole category
