---
title: "Per-Prompt RAG Made Our Coding Agent Worse. Real Memory Cut Corrections 31–58% — and Cost 39%"
authors: [nicoloboschi]

date: 2026-07-29T12:00
tags: [coding-agents, memory, claude-code, opencode, codex, benchmark, knowledge-pages]
image: /img/blog/coding-agents-memory.png
draft: true
---

We benchmarked long-term project memory for coding agents on the thing benchmarks never measure: how many times a human has to step in and correct the agent. The results rebuilt our product.

**TL;DR** —
- **Per-prompt RAG injection — the memory pattern most of the ecosystem ships — made the agent *worse than no memory at all*** (1.06 vs 0.97 corrections per task on our suite).
- Session-level memory synthesis + curated knowledge pages cut corrections **31%** out of the box (empty bank, fully automatic ingestion) and **44%** on matured banks; **58%** with a stronger model.
- Model cost went **down 39%** — memory replaces exploration, it doesn't add overhead.
- Everything ships today: one plugin for opencode, Claude Code, Codex CLI, Gemini CLI, Cursor CLI — plus **knowledge pages**, live for every Hindsight bank.

<!-- truncate -->

---

## One task, both runs

Here's the moment the whole project exists for. (Reconstructed from a benchmark task: the decision, the failure sequence, and the injected memory are the real ones; the dialogue is condensed.)

The agent gets a bug report: *"After last night's deploy, transient upstream errors are locking customer accounts — users hit one failure and then can't get back in."* Real codebase, ~1,600 commits of history, full test suite, standard tools.

**Without memory**, the agent does what a competent engineer new to the codebase would do: finds the retry wrapper, sees it retrying nothing but 5xx, and extends it with the industry-standard heuristic — retry the "retryable" 4xxs too. The visible repro passes. The harness runs the *hidden* test — the one encoding what the team actually decided — and it fails: this project retries **exactly** `429` and `408` from the 4xx range, nothing else. The agent's second attempt keeps 429 but drops the rest — still wrong: it dropped `408` too. There is no standard to reason from here, because the rule isn't a standard — it's a *decision*, made after a specific incident. Each failure comes back as raw pytest output with no hint; each retry is a **correction**, a moment where, on a real team, a human would have stopped their own work to un-stick the agent.

**With memory**, the session's first prompt triggers one deep synthesis over the project's history, and the agent's context opens with:

> 🧠 From Hindsight memory: retries apply to any 5xx plus **exactly `429` and `408`** from the 4xx range; every other 4xx fails fast. Settled after the 401-retry storm locked customer accounts — an earlier draft that retried all 4xx was explicitly reversed.

One attempt. Tests pass, hidden test included. Notice what retrieval had to do: the bug report says *deploy*, *locking*, *accounts*; the deciding conversation months earlier was about *retry policy* and mentions none of those words. Nothing in the symptom's vocabulary points at the answer — which is exactly why the naive approach fails, and that's the next section.

Across the 33-task suite that difference compounds: **10 fewer interruptions per 33 tasks**. If your team pushes twenty agent tasks a day, that's roughly **six times a day someone doesn't get pulled out of their own work** — thirty times a week — before counting the 39% model-cost drop ($0.79 → $0.48 per task) from all the exploration the agent no longer does.

## "But you put the answer in the database"

Let's meet the obvious objection head-on, because it's the right question: *of course memory wins — you seeded the deciding conversation into the memory bank.*

Yes. Deliberately. That's not a trick; it's the realistic premise. On every real team, the deciding context **already exists** — in git history, code reviews, Slack threads, closed tickets. The open question was never whether the answer exists somewhere; it's whether a system can **find it when it matters**: across a vocabulary gap (the symptom shares no words with the decision), buried among ~1,600 real commits and **40 decoy conversations** — long, plausible, codebase-related discussions that decide nothing relevant — and, hardest of all, **without serving a superseded version**. Two tasks plant a decision that was amended by a later conversation; a memory that confidently returns the outdated rule is worse than one that stays silent. Retrieval has to rank, reconcile, and know when the team changed its mind. That's the test.

And the flip side of the premise holds too: our first architecture had full access to the same seeded answers *and still lost to no memory at all*. Having the answer in the database is evidently not the same as winning.

## The finding that should worry the ecosystem

The "obvious" memory integration — embed the user's prompt, retrieve similar memories, inject them — is what most agent-memory products ship, and it's what one of our own integrations did. On the suite it scored **1.06 corrections per task versus vanilla's 0.97 (35 total vs 32). Worse than no memory.**

The autopsy: half our hard tier is deliberately *symptom-distant*, like the retry task above. Similarity search, faced with a symptom that shares no vocabulary with the cause, returns snippets that merely sound alike — plausible-looking, confidently injected, wrong. The agent then iterates with distracting context attached, which is measurably worse than iterating with none. We had shipped variants of this pattern. The benchmark killed it in an afternoon, and we'd encourage anyone evaluating an agent-memory product to run this exact test: **per-prompt similarity injection on symptom-distant bugs**. It's where the demos go to die.

What replaced it is a two-level architecture, and the benchmark chose both levels:

**Reflect — deep synthesis, once per session.** On the session's first prompt, an agentic reasoning pass works over the *whole* memory bank: it follows the connection from symptom to decision across the vocabulary gap, checks for later amendments, and returns the exact rule with its literal values quoted verbatim. It takes seconds, not milliseconds — so you pay for depth exactly once, and the answer rides along for the rest of the session. It's not a hidden wait, either: the plugin shows you the assignment and the finding while it happens —

```
Hindsight · goal: recall this repo's past decisions about “transient errors locking accounts”
↳ Retries apply to any 5xx plus exactly 429 and 408; every other 4xx fails fast…
```

— and it's hard-capped (25s) so a slow day degrades to a normal memoryless session, never a hung one. Every turn after the first is instant: the synthesis is cached for the session.

**Knowledge pages — organized memory, on demand.** For everything that doesn't warrant a full synthesis, the second level. It's the feature we're launching today across Hindsight, and it deserves its own section.

## Knowledge pages: the wiki nobody has to write

Raw memory is thousands of extracted facts — precise, but shapeless. A **knowledge page** is the shape: a curated, human-readable living document synthesized *from* those facts, re-synthesized server-side as new facts arrive. It's the difference between a search index and a wiki — except nobody writes the wiki, and it can't go stale.

When the plugin ingests a repo, it seeds a small fixed taxonomy — **component map**, **core concepts**, **conventions and patterns**, **key decisions and rationale**, **initiatives and enhancements**. A few sessions into a real project, the decisions page reads like the document your best senior engineer never had time to write:

> **Key decisions and rationale**
>
> **Retry policy.** Retries apply to any 5xx plus exactly `429` and `408` from the 4xx range; every other 4xx fails fast. Settled after the 401-storm incident locked customer accounts — amends an earlier draft that retried all 4xx.
>
> **Export sanitization.** The ERP importer rejects quoted fields outright (it predates RFC 4180), so descriptions are sanitized before export: embedded delimiters are replaced with `" / "`. Do not "fix" this by quoting — that was tried, and rolled back…

What keeps the pages coherent is routing at *extraction* time: as facts are pulled from commits and sessions, the durable ones are labeled by knowledge tier — decision, convention, component, concept, initiative — and each page synthesizes from exactly its tier. Transient facts (a passing test, a one-off command, a debugging dead end) get no label on purpose: they're useful to deep synthesis, poison to a curated page. When an agent starts a genuinely new piece of work, it registers the initiative and that initiative gets its own page, accruing progress across sessions.

Agents reach pages through a **hybrid search tool** — full-text + semantic, fused server-side, ranked results with snippets in tens of milliseconds — plus a read-page tool. We tried auto-injecting the best-matching page on every turn first; it benchmarked fine and *felt* dishonest — answer "yes" and the UI implies the agent researched three documents. Retrieval you didn't ask for reads as noise; retrieval the agent visibly *chooses* reads as work. So pages are pulled, not pushed, and every tool call is visible in the transcript.

One rule ties it together: **attribution**. When memory shapes an answer, the agent must credit it inline — `🧠 From Hindsight memory (Key decisions and rationale): …` — and must *not* credit memory that didn't contribute. You always know which part of an answer came from your project's history rather than from the code in front of it.

And pages aren't a plugin trick — they're first-class in Hindsight as of today, for every bank: browse and edit them in the dashboard's new Knowledge view (a proper document editor, tags inline), query them over the API, search them with the hybrid endpoint, ship a whole taxonomy in one shot with a bank template. The coding-agents plugin is simply the first integration built on them — its entire bank setup, pages included, is a single template import.

## Your data, your machine, your server

Three questions every evaluating engineer asks, answered plainly:

**Where does the data go?** The plugin talks to *your* Hindsight server — self-hosted (a Docker container) or your cloud instance — configured in one file. Commit history and session transcripts go there and nowhere else; the LLM calls that extract and synthesize memory are made by *your server* with *your* configured provider and keys. The plugin itself adds read-only retrieval into the agent's context; a per-repo `retainSessions: false` keeps any repo's sessions out of memory entirely, and `disabled` opts a repo out wholesale.

**What does day one cost?** Ingestion is deliberately bounded: the initial seed is the recent commit history as one cheap aggregated document (minutes, not hours, even on large repos), a short read-only structure survey with a hard spend cap, and then *progressive* deepening — a bounded batch of full commit diffs per session, newest first, so a 50k-commit monorepo never triggers a big-bang ingest. The readiness contract is observable (`synced: true`, surfaced in the session banner as `git in sync`), and the depth is one setting (`gitIngest: message | full | none`).

**Will my first prompt hang?** No — reflect's seconds happen once per session, on-screen (you watch the goal and the finding stream past, as above), hard-capped, fail-open. Every subsequent turn adds zero retrieval latency: synthesis is cached, and page search runs only when the agent chooses to call it.

## The numbers, and which one to repeat

The headline run measures the **product, end to end**: the harness deletes the memory bank before each task and lets the plugin's own automatic pipeline rebuild it from empty — ingestion, extraction, labeling, pages — waiting on the public sync-status contract before the agent types a word. No hand-built index anywhere.

| Condition | Corrections / task | vs vanilla |
|---|---|---|
| Vanilla (no memory), mean of 3 runs | 0.97 | — |
| **Memory, out-of-box** (bank built from empty, automatically) | **0.67** | **−31%** |
| **Memory, matured banks** (history accrued from prior sessions) | **0.55** | **−44%** |

That's OpenCode + Gemini 3.5 Flash — a deliberately modest model, because we wanted the out-of-box measurement on an affordable stack. 33/33 tasks solved in every memory run, cost down 39%.

**How grading works**, compactly, since "corrections" is our metric and you should be able to reconstruct it: the agent attempts a fix; the full test suite runs, *including the hidden test it has never seen*; any failure output goes back to the agent verbatim — no hints, no human — and it retries, up to five rounds. Corrections = retries. Grading is deterministic pytest end to end; there is no LLM judge anywhere. Both arms run the identical loop.

And one phrase above deserves its backstory: *injection verified on all 33*. Early in this project a "memory" run scored suspiciously close to vanilla, and when we dug in, retrieval had been silently failing the whole time — we had benchmarked a placebo and nearly reported it. Since then every memory run records per-task, machine-checkable proof that retrieval actually reached the agent's context, and a run without that proof is discarded as *no data*, not counted as a result.

**If you're on Claude Code with Sonnet — probably most readers — your number is likely bigger.** Our earlier five-run-per-arm campaign on that stack cut corrections **58%** (0.91 → 0.38 per task). One honest asymmetry: that campaign predates the final plugin — banks were pre-built by the harness rather than by today's out-of-box pipeline, so it's the *mechanism* under looser conditions, not the strict product measurement the table above uses. We're re-running it under the current methodology; the mechanism is identical, stronger models convert a retrieved decision into a first-try fix more reliably, and the pattern we expect to hold is: *the better the agent, the more its remaining failures are exactly the non-guessable decisions memory carries.*

The gap between the two memory rows is the quiet headline: 0.67 from a cold start, 0.55 once the bank has lived with the project. Sessions write themselves back — compacted transcripts, the plugin's own injections carefully stripped so the bank never eats its own output — so **the agent gets better at your repository just by working in it.**

## Honest limitations

One suite, one codebase family, built deliberately from tasks where memory *can* matter — it quantifies the last-mile-decision problem, not general coding ability; on tasks fully solvable from code, expect parity, not gains. The out-of-box row is a single verified run (an n=3 refresh is in progress; the prior architecture's n=3 campaign landed in the same 22–27 range on matured banks). Dataset, harness, per-task results, and the full hardening journal — including the experiments where our own ideas lost, because a benchmark that only ever confirms you is a mirror, not an instrument — go live at [agentmemorybenchmark.ai](https://agentmemorybenchmark.ai) in August 2026.

## The backstory, briefly

We didn't set out to write a benchmark paper. Customers kept asking "how much does memory actually help?", and all we had was anecdote — plus five integrations that had grown independently, each injecting memory its own way, with a manual ingestion CLI nobody ran twice. There was no single behavior called "the plugin"; there were five behaviors wearing a trench coat. We reached for SWE-bench first and hit two walls: it grades a one-shot patch while real agent work is an iterative *walk* (ask, fail, retry — the pain is the interruptions, not the final patch), and its tasks are solvable from the repo alone, so a perfect memory and no memory converge. A benchmark where your product can't win isn't flattering to run — but more importantly, it isn't *informative*. So we built the suite above, let it kill our own architecture, and shipped what survived: one plugin, one opinionated path, memory that builds itself, and knowledge pages for everything the agent shouldn't have to re-learn.

## Try it on the repo that knows things your agent doesn't

```bash
npm install -g hindsight-coding-agents && hindsight-coding-agents install
```

One command detects your agents — opencode, Claude Code, Codex CLI, Gemini CLI, Cursor CLI — and wires each natively. Point it at a [Hindsight server](https://vectorize.io/hindsight) in `~/.hindsight/coding-agent.json`, open a repo, and watch it introduce itself:

```
Hindsight is tracking the decisions, conventions and history of this repo
  ↳ memory bank “coding-agent::your-repo” · git in sync
```

And whether or not you run coding agents: **knowledge pages are live today for every Hindsight bank.** Open your dashboard, look at the Knowledge tab, and meet the documents your memory has been waiting to write. Docs: [coding agents integration](/sdks/integrations/coding-agents).
