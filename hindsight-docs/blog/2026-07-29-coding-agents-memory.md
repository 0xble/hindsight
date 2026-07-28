---
title: "Does Memory Actually Help Coding Agents? We Built a Benchmark to Find Out — and Rebuilt Our Plugin Because of It"
authors: [nicoloboschi]

date: 2026-07-29T12:00
tags: [coding-agents, memory, claude-code, opencode, codex, benchmark, knowledge-pages]
image: /img/blog/coding-agents-memory.png
draft: true
---

We ship long-term memory for AI agents, so people kept asking us a fair question: *does memory actually make a coding agent better, or does it just feel like it should?* We didn't have a number. Getting one took most of a month, forced us to build a benchmark nobody had, killed an architecture we had already shipped, and consolidated five scattered integrations into one plugin with one opinionated path. Along the way it pushed us to build the feature we're launching today alongside the plugin — **knowledge pages**, living documents your memory bank writes about itself. This is the full story, ending at **−31% corrections out of the box** — measured from a completely empty memory bank.

<!-- truncate -->

---

## The question we couldn't answer

By early summer we had Hindsight integrations for several coding agents — a Claude Code hook here, an opencode plugin there, a half-wired Codex experiment in a branch. Users liked them. Demos were great: you'd ask the agent about a bug, memory would surface the exact Slack-thread decision that explained it, and the room would nod. And then a customer would ask the only question that matters — *"how much does this actually help?"* — and everything we could offer was anecdote.

The embarrassing part wasn't the missing number. It was that when we sat down to produce one, we realized we couldn't even say precisely what we'd be measuring. The integrations had grown independently: one recalled memories on every prompt, another synthesized once at session start, one injected into the system prompt, another into user-message context. Ingestion was a manual CLI you had to remember to run — which meant in practice most banks were seeded once at demo time and never again. There was no single behavior called "the plugin." There were five behaviors wearing a trench coat.

So the project became two intertwined problems, and this post is about both: **build a measurement that can't lie to us**, and **let it decide what the product should be**.

## Why not SWE-bench

The obvious starting point was SWE-bench: real repositories, real issues, the field's standard yardstick. We started there — and hit two walls that in hindsight (sorry) define the whole problem.

**It doesn't reflect how people actually use coding agents.** SWE-bench is one-shot: the agent reads an issue, emits a patch, the patch is graded, the end. Nobody works with a coding agent that way. You ask, the agent tries, the tests push back, it tries again; you nudge, it revises. Real agent work is a *walk*, not a shot. And the pain of a weak agent isn't a failed patch at the end of the walk — it's every intermediate step where a human has to stop what they're doing and correct course. A benchmark that can't see the iteration loop is structurally blind to the thing memory is supposed to fix.

**Memory has nothing to add by construction.** SWE-bench tasks are solvable from the repository: the issue text plus the code contain the answer, and frontier models have additionally seen much of that code during training. A memory system can only earn its keep on tasks where the deciding context *is not in the repo*. On SWE-bench, a perfect memory and no memory should converge — and a benchmark where your product can't win isn't flattering to run, but more importantly it isn't *informative*.

What we actually needed was a benchmark for the moment every senior engineer recognizes: the fix that looks right, passes the repro, and is still wrong — because two years ago, in a code review or a Slack thread, the team decided something the code doesn't say.

## The dataset: bugs you cannot guess

So we built our own suite: 33 bug-fix tasks hosted inside a real open-source codebase (`boltons`), with its ~1,600 real commits left in place as retrieval noise and 40 decoy conversations mixed into the history — long, plausible, codebase-related discussions that decide nothing relevant. Retrieval has to *rank*, not just fetch.

The design rules came straight from the two SWE-bench walls:

**Every task hinges on a non-guessable, project-specific decision.** The visible bug report reproduces a real failure, and the *obvious* fix passes that repro. Then a **hidden test** — which the agent never sees — checks the decision the team actually made: which of the 4xx status codes are retryable and which fail fast; how a discount rounds at the boundary; which record wins a dedupe merge and why; the exact jitter window on a retry backoff. Multi-part policies with three or more interacting constraints, where each test failure reveals only a slice of the rule. If you don't know the decision, you can iterate toward it only painfully; if you know it, you fix it once.

**The rationale lives where teams really keep it.** For 15 tasks it's in a past developer conversation. For 16 it's in a commit message's reasoning, buried in those 1,600 commits. And for 2 tasks — the nastiest — it's in a conversation that was **amended by a later one**: the team decided X, then changed to Y a week later. Those two tasks exist to punish memory systems that can't tell a superseded decision from a final one; a memory that confidently serves the outdated rule is worse than one that stays silent.

**Grading mirrors the walk.** The agent works in the real loop: attempt → tests run → failures go back verbatim (no hints) → it retries, capped at five rounds. The primary metric is **corrections**: how many times the fix came back wrong. Each correction is a moment a human would have been interrupted. Grading is deterministic pytest — no LLM judge anywhere in the pipeline.

**And the harness assumes we might fool ourselves.** This rule we learned the hard way: an early "memory" run scored suspiciously close to vanilla, and when we dug in, retrieval had been silently failing the whole time — we'd benchmarked a placebo. Since then, every memory run records per-task, machine-checkable proof that retrieval actually reached the agent's context, and a run without that proof is discarded as *no data* rather than counted as a data point. Both arms get identical repos, identical tools, full git history, and the identical feedback loop; the memory arm adds only read-only retrieval.

## What measurement did to the product

Then we started running it, and the benchmark began making product decisions for us — quickly, and not gently.

### Per-prompt recall lost to *nothing*

The "obvious" memory integration — retrieve similar memories on every prompt and inject them — is what most of the ecosystem builds, and it's what one of our own integrations did. On the suite it scored **35 total corrections versus vanilla's 32**. Worse than no memory at all.

The autopsy was instructive. Half our hard tier is deliberately *symptom-distant*: the bug report says "the nightly export corrupts rows with commas," while the deciding conversation two months earlier was about CSV quoting policy and never mentions exports or corruption. Similarity search, faced with that gap, returns snippets that merely share vocabulary with the symptom — plausible-looking, confidently injected, and wrong. The agent now iterates with distracting context attached, which is measurably worse than iterating with none. We had shipped variants of this pattern. The benchmark killed it in an afternoon.

### Synthesis, once per session, is what works

Hindsight has a second retrieval mode we call **reflect**: instead of fetching similar snippets, an agentic reasoning pass works over the whole memory bank — following the connection from the symptom to the decision that explains it, across vocabulary gaps, checking for later amendments — and returns a synthesized answer with the exact rule and its literal values quoted verbatim. On the amended-conversation tasks, reflect states the *final* rule and explicitly retires the superseded one.

Reflect brought the suite from 32 corrections to **22–27 across runs**. It's slow — seconds, not milliseconds — which turned into a design constraint we ended up liking: you pay for depth **once**, on the session's first prompt, and the synthesized answer rides along for the whole session. When it runs, the plugin says so, visibly:

```
Hindsight · goal: recall this repo's past decisions about “why does the retry policy lock accounts?”
↳ The team decided retries apply to any 5xx plus exactly 429 and 408 from the 4xx range…
```

### Knowledge pages: the middle of the memory spectrum

That left a gap. A session raises plenty of questions that don't deserve a full synthesis but die under raw recall: *what are the components of this repo? what's our error-handling convention? what was decided about the retry policy, roughly? what's currently being built?* We needed something organized like reflect but fast like recall.

That something is **knowledge pages** — the feature we're launching today across Hindsight, and the second pillar of the plugin. They deserve a proper introduction.

A knowledge page is a *living document* your memory bank writes about itself. Raw memory is thousands of extracted facts — precise, but shapeless. A knowledge page is the shape: a curated, human-readable document synthesized *from* those facts, that stays current because the server re-synthesizes it whenever consolidation brings new facts in. It's the difference between a search index and a wiki — except nobody has to write the wiki, and it can't go stale.

When the plugin ingests a repo it seeds a small, fixed taxonomy: a **component map**, **core concepts**, **conventions and patterns**, **key decisions and rationale**, and **initiatives and enhancements**. A few sessions into a real project, the decisions page reads like the document your best senior engineer never had time to write:

> **Key decisions and rationale**
>
> **Retry policy.** Retries apply to any 5xx plus exactly `429` and `408` from the 4xx range; every other 4xx fails fast. Settled after the 401-storm incident locked customer accounts — see the amendment: an earlier draft retried all 4xx.
>
> **CSV escaping.** Fields are quoted only when they contain the delimiter, quotes are doubled rather than backslash-escaped, for round-trip compatibility with the ERP importer…

And they're not a plugin-only trick — pages are first-class in Hindsight. Every bank gets them: browse and edit them in the dashboard's new Knowledge view (a proper document editor, tags inline, the bank's tree at your side), query them over the API, search them with the new hybrid endpoint, or ship a whole taxonomy in one shot with a bank template. The coding-agents plugin is simply the first integration built *on* them — its entire bank setup, pages included, is a single template import.

The routing that keeps them coherent happens at *extraction* time. As facts are extracted from commits and conversations, the extractor labels the durable ones with a knowledge tier — this is a `decision`, this is a `convention`, this describes a `component` — and each page's synthesis draws from exactly the facts routed to its tier. Most facts get no label at all, deliberately: a passing test, a one-off command, a debugging dead end are *operational* memory, useful to reflect but poison to a curated page. The taxonomy also grows sideways: when an agent starts a genuinely new piece of work, it registers the initiative, and that initiative gets its own page that accrues progress across sessions.

Agents reach the pages through a **hybrid search tool** — full-text and semantic retrieval fused server-side, returning ranked pages with relevance snippets in tens of milliseconds — plus a tool to read any page in full. We tried the other obvious design first: automatically injecting the best-matching page content on every turn. It benchmarked fine and *felt* terrible — answer "yes" to a question and the UI implies the agent just did research on three documents. Retrieval you didn't ask for reads as noise; retrieval the agent visibly *chooses* reads as work. So page knowledge is pulled, not pushed: the agent calls the search tool when a question warrants it, the tool call is visible in the transcript, and the spectrum ends up clean — **reflect** for the deep once-per-session synthesis, **pages** for fast organized knowledge on demand, and nothing in between to muddy either.

One more rule ties the room together: **attribution**. When memory shapes any part of an answer, the agent is required to credit it inline — `🧠 From Hindsight memory (Key decisions and rationale): …` — and equally required *not* to credit memory that didn't contribute. You always know which part of an answer came from your project's history rather than from reading the code, and false credit is defined as a wrong answer.

### Memory that builds itself

The last thing the benchmark work exposed wasn't an algorithm problem — it was an operations problem. Our ingestion was a CLI. In the benchmark harness we could script it; in the real world, nobody runs an ingestion CLI twice. Banks went stale the day after the demo.

So we deleted it. The final plugin builds memory entirely in the background. The first time an agent opens a repo, it seeds the bank from the commit-message history and runs a short, read-only survey of the codebase structure — and tells you, in the session banner, that it's doing so. Every session start after that, an idempotent background engine tops the bank up: new commits (progressively deepening into full per-commit diffs, newest first, a bounded batch per session so cost stays flat), new conversations, refreshed pages. Sessions write *themselves* back — compacted transcripts where tool calls become one-line actions, with the plugin's own injected memory carefully stripped so the bank never eats its own output. There is no setup step, no sync button, no export. Readiness is an observable contract (`synced: true`), not a hope.

And the five scattered integrations became one package — opencode, Claude Code, Codex CLI, Gemini CLI, Cursor CLI — with one runtime, one config file, and a one-command installer:

```bash
npm install -g hindsight-coding-agents && hindsight-coding-agents install
```

Open a repo and it introduces itself:

```
Hindsight is tracking the decisions, conventions and history of this repo
  ↳ memory bank “coding-agent::your-repo” · git in sync
```

## The final numbers

For the headline run we measured the *product*, not a hand-built index. The harness deletes the memory bank before each task and lets the plugin's own automatic pipeline rebuild it from empty — ingestion, extraction, labeling, pages — waiting on the plugin's public sync-status contract before the agent types a word.

| Condition | Corrections / task | vs vanilla |
|---|---|---|
| Vanilla (no memory), mean of 3 runs | 0.97 | — |
| **Memory, out-of-box** (bank built from empty, automatically) | **0.67** | **−31%** |
| **Memory, matured banks** (history accrued from prior sessions) | **0.55** | **−44%** |

OpenCode + Gemini 3.5 Flash; 33/33 tasks solved in every memory run; injection verified on all 33; per-task model cost *down* 39% ($0.79 → $0.48), because one targeted retrieval replaces a lot of exploratory reading. In an earlier campaign with a stronger model (Claude Code + Sonnet, five runs per arm), the same mechanism cut corrections **58%** — a pattern we expect to hold: the stronger the agent, the more its *remaining* failures are precisely the non-guessable decisions memory carries.

The fresh-versus-matured gap is the result we find most satisfying. 0.67 corrections per task from a cold start; 0.55 once the bank has lived with the project. Because sessions write themselves back, **the agent gets better at your repository just by working in it** — the compounding is automatic, and it's the closest thing to "onboarding" an AI teammate that we know how to build.

## Honest limitations

One suite, one codebase family. The out-of-box row is a single verified run (an n=3 refresh is in progress; the previous architecture's n=3 campaign landed in the same 22–27 range). The dataset deliberately concentrates on tasks where memory *can* matter — it quantifies the last-mile-decision problem, not general coding ability; on tasks fully solvable from code, expect parity, not gains. Dataset, harness, per-task results, and the full hardening journal — including the experiments where our own ideas lost, because a benchmark that only ever confirms you is a mirror, not an instrument — will be public at [agentmemorybenchmark.ai](https://agentmemorybenchmark.ai).

## Try it on the repo that knows things your agent doesn't

```bash
npm install -g hindsight-coding-agents && hindsight-coding-agents install
```

Point it at a [Hindsight server](https://vectorize.io/hindsight) in `~/.hindsight/coding-agent.json`, open a repo, and watch it introduce itself. Works with opencode, Claude Code, Codex CLI, Gemini CLI, and Cursor CLI — one plugin, one path. Docs: [coding agents integration](/sdks/integrations/coding-agents).

And whether or not you run coding agents: **knowledge pages are live today for every Hindsight bank.** Open your dashboard, look at the Knowledge tab, and meet the documents your memory has been waiting to write.
