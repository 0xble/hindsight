---
title: "Your Coding Agent Keeps Making the Same Mistake. Ours Stopped: −31% Corrections With Long-Term Project Memory"
authors: [nicoloboschi]

date: 2026-07-29T12:00
tags: [coding-agents, memory, claude-code, opencode, codex, benchmark, knowledge-pages]
image: /img/blog/coding-agents-memory.png
draft: true
---

We gave coding agents long-term project memory — automatic, zero-setup, one plugin for opencode, Claude Code, Codex CLI, Gemini CLI, and Cursor CLI — and measured what happens on a benchmark of bugs whose correct fix *cannot be guessed from the code*. Starting from a completely empty memory bank, corrections dropped 31%. On banks matured by real usage: 44%. With a stronger model: 58%.

<!-- truncate -->

---

## The mistake your agent keeps making

Coding agents are good at what's *in* the code. They fail on what isn't.

Most of a real fix is derivable from the codebase — but the last mile often hinges on a **project-specific decision** that lives nowhere in the source: a rounding rule someone settled in a code review, a retry allowlist agreed in Slack, a tie-break policy explained in a commit message two years ago. Without that context an agent produces a plausible-but-wrong fix, the tests (or a human) push back, it tries again. Every round of that loop is a developer interruption.

Teams already *have* the missing context. It's in git history and past conversations. It's just not in front of the agent when it starts typing.

## What we built

[`hindsight-coding-agents`](/sdks/integrations/coding-agents) is one plugin that puts a repository's accumulated decisions in front of any of five coding agents, with no setup step at all:

```bash
npm install -g hindsight-coding-agents && hindsight-coding-agents install
```

That's the whole installation — it detects the agents on your machine and wires each natively. From there, everything is automatic:

**Memory builds itself.** The first time an agent opens a repo, the plugin seeds a memory bank from the commit history and runs a short read-only survey of the codebase structure. Every session after that, a background engine keeps the bank current — new commits, new sessions — deepening progressively (recent commits first, full diffs) without a big-bang ingest. There is no CLI to run, no export step, no sync button. Open a repo and it says so:

```
Hindsight is tracking the decisions, conventions and history of this repo
  ↳ memory bank “coding-agent::your-repo” · git in sync
```

**One deep synthesis per session.** On your first prompt, Hindsight *reflects*: an agentic reasoning pass over the whole bank that connects your task to the past decision that explains it — and returns the exact rule with its literal values, not a pile of similar-looking snippets. That answer rides along for the rest of the session.

**Knowledge pages for everything else.** The bank continuously curates a small set of living documents — component map, conventions, key decisions and their rationale, active initiatives — rebuilt server-side as new facts arrive. Agents query them through a hybrid (full-text + semantic) search tool whenever a question calls for project knowledge, and read any page in full. Every session also writes itself back into the bank, so the memory compounds: decisions made *with* the agent today are context *for* the agent tomorrow.

**And you can see it working.** When memory shapes an answer, the agent credits it inline — `🧠 From Hindsight memory (Key decisions and rationale): …` — so you always know which part came from your project's history rather than from reading the code.

## The benchmark

Claims about memory need adversarial measurement, so we built a benchmark designed to resist wishful results — 33 bug-fix tasks hosted in a real open-source codebase (`boltons`, ~1,600 real commits as retrieval noise, plus 40 decoy conversations):

- Every task's correct fix hinges on a **non-guessable decision**: the obvious fix passes the visible repro but fails a **hidden test**.
- The deciding rationale lives in a past conversation (15 tasks), a real commit's rationale (16), or a conversation **amended by a later one** (2) — that last category catches memories that can't tell a superseded decision from a final one.
- Grading is deterministic — pytest, no LLM judge. The primary metric is **corrections**: the agent's fix fails the tests and it must retry. Exactly the moments a human would have to step in.
- Both arms are the same agent with the same tools and full git history; the memory arm adds only read-only retrieval. Every memory run records per-task proof that retrieval actually reached the agent — a silently memory-less run cannot masquerade as a memory result.

For the headline run we went further than we had to: the harness **deletes the memory bank before each task** and lets the plugin's own automatic pipeline rebuild it from empty — ingestion, extraction, knowledge pages, the works — waiting on the plugin's public sync-status contract before the agent starts. What's measured is the out-of-box product, not a hand-tuned index.

## Results

| Condition | Corrections / task | vs vanilla |
|---|---|---|
| Vanilla (no memory), mean of 3 runs | 0.97 | — |
| **Memory, out-of-box** (bank built from empty, automatically) | **0.67** | **−31%** |
| **Memory, matured banks** (history accrued from prior sessions) | **0.55** | **−44%** |

OpenCode + Gemini 3.5 Flash, 33/33 tasks solved in every memory run, injection verified on all 33. Cost went *down* 39% ($0.79 → $0.48 per task) — memory replaces exploration with one targeted retrieval. In our earlier campaign with a stronger model (Claude Code + Sonnet, 5 runs per arm), the same mechanism cut corrections **58%** — the better the agent, the more its residual failures are exactly the non-guessable decisions memory addresses.

Two results deserve their own sentences:

**Memory compounds.** The gap between 0.67 (fresh) and 0.55 (matured) is the product thesis in one number: the longer a repo lives with memory, the better its agent gets — automatically, because sessions write themselves back.

**Retrieval quality is the whole game.** Mid-project we tested a variant that replaced the per-session synthesis with straight per-prompt similarity search. It scored *worse than no memory at all* (35 total corrections vs vanilla's 32): on symptom-distant bugs, similar-looking snippets are distracting noise. The architecture that wins is synthesis where depth matters (once per session) plus curated knowledge pages where speed matters (every question) — not raw recall everywhere.

## Honest limitations

One benchmark, one codebase family, and the out-of-box numbers are single verified runs (an n=3 refresh is in progress; the prior architecture's n=3 landed in the same range). The suite is deliberately built from tasks where memory *can* matter — it measures the last-mile-decision problem, not general coding ability. Dataset, harness, per-task results, and the full hardening journal will be public at [agentmemorybenchmark.ai](https://agentmemorybenchmark.ai), including the runs where our own ideas lost.

## Run it on your repo

The strongest test isn't our benchmark — it's your repository, whose history is full of decisions your agent keeps not knowing.

```bash
npm install -g hindsight-coding-agents && hindsight-coding-agents install
```

Point it at a [Hindsight server](https://vectorize.io/hindsight) (self-hosted or cloud) in `~/.hindsight/coding-agent.json`, open a repo, and watch the banner. Docs: [coding agents integration](/sdks/integrations/coding-agents).
