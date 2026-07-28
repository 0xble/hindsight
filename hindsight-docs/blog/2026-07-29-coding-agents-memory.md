---
title: "Does Memory Actually Help Coding Agents? We Built a Benchmark to Find Out — and Rebuilt Our Plugin Because of It"
authors: [nicoloboschi]

date: 2026-07-29T12:00
tags: [coding-agents, memory, claude-code, opencode, codex, benchmark, knowledge-pages]
image: /img/blog/coding-agents-memory.png
draft: true
---

We ship long-term memory for AI agents, so people kept asking us a fair question: *does memory actually make a coding agent better, or does it just feel like it should?* We didn't have a number. Getting one forced us to build a benchmark nobody had, throw away an architecture we liked, and consolidate five scattered integrations into one plugin. This is the story, ending at −31% corrections out of the box — measured from an empty memory bank.

<!-- truncate -->

---

## The question we couldn't answer

By early summer we had Hindsight integrations for several coding agents — a Claude Code hook here, an opencode plugin there, each wired a little differently, each injecting memory its own way. Users liked them. Demos were great. And when a customer asked "how much does this actually help?", everything we could say was anecdote.

Worse: because the integrations were scattered, there wasn't even a single behavior to measure. One plugin recalled memories on every prompt; another synthesized once at session start; ingestion was a manual CLI you had to remember to run. If we wanted a real number, we first needed a real, single answer to "what does the plugin *do*?"

So the project became two intertwined problems: **build a measurement that can't lie to us**, and **use it to decide what the product should be**.

## Why not SWE-bench

The obvious starting point was SWE-bench: real repos, real issues, standard in the field. We started there and hit two walls.

**It doesn't reflect how people use coding agents.** SWE-bench is one-shot: the agent emits a patch, the patch is graded, the end. Nobody works with a coding agent that way. You ask, the agent tries, the tests push back, it tries again — you *iterate*. The pain of a weak agent isn't a failed patch; it's the third time you have to step in and correct it. A benchmark that can't see the iteration loop can't see the thing memory is supposed to fix.

**Memory has nothing to add.** SWE-bench tasks are, by construction, solvable from the repository — the issue text plus the code contain the answer (and frontier models have seen much of it during training). A memory system can only shine on tasks where the deciding context *isn't in the repo*. On SWE-bench, a perfect memory and no memory should score the same.

## The dataset: bugs you can't guess

So we built our own suite — 33 bug-fix tasks hosted inside a real open-source codebase (`boltons`, with its ~1,600 real commits left in place as retrieval noise, plus 40 decoy conversations). The design rules came directly from the two SWE-bench walls:

**Every task hinges on a non-guessable, project-specific decision.** The visible bug report reproduces a failure; the *obvious* fix passes that repro — and then fails a **hidden test** that encodes the decision the team actually made: a rounding rule, a retry allowlist, a tie-break policy, exact literal values. The rationale lives where teams really keep it: in a **past developer conversation** (15 tasks), in a **commit message's reasoning** (16), or — nastiest — in a conversation that was **amended by a later one** (2 tasks, which punish any memory that can't tell a superseded decision from the final one).

**Grading mirrors real usage.** The agent works in a loop: attempt → tests run → failures go back to it verbatim (no hints) → it retries, capped at five rounds. The primary metric is **corrections** — how many times the fix came back wrong. That's the count of moments a human would have been interrupted. Grading is deterministic pytest; no LLM judge anywhere.

**The harness assumes we might fool ourselves.** Every memory run records per-task proof that retrieval actually reached the agent — early on we caught a "memory" run that had silently failed retrieval and scored like vanilla, which taught us that a benchmark without injection verification is a benchmark of nothing. Both arms get identical repos, tools, full git history, and the identical feedback loop; the memory arm adds only read-only retrieval.

## What measurement did to the product

Then we started running it, and the numbers immediately started making product decisions for us.

**Per-prompt recall — the "obvious" memory integration — scored worse than no memory at all.** 35 total corrections versus vanilla's 32. On bugs whose symptom is worded nothing like the original decision (half our hard tier, on purpose), similarity search surfaces plausible-looking snippets that are simply *distracting*, and the agent iterates past the right answer with confidence. We had shipped variants of this pattern. The benchmark killed it in one afternoon.

**Deep synthesis, once per session, is what works.** Hindsight's *reflect* — an agentic reasoning pass over the whole bank that connects the task to the past decision explaining it and returns the exact rule with its literal values — brought the suite to 22–27 corrections across runs. Slow (seconds), so you do it once on the session's first prompt and let the answer ride along; the cost profile works because the depth only has to be paid once.

**For everything in between, knowledge pages.** Sessions raise plenty of smaller questions — "what are the components here?", "what's our error-handling convention?" — where a full synthesis is overkill and raw recall is noise. The bank continuously curates a handful of living documents (component map, conventions, key decisions with rationale, active initiatives), rebuilt server-side as facts arrive, and the agent queries them through a hybrid full-text + semantic search tool. Organized like synthesis, fast like search. When any of it shapes an answer, the agent credits it inline — `🧠 From Hindsight memory (Key decisions and rationale): …` — so you can always tell what came from your project's history.

**And nobody runs an ingestion CLI, so we deleted ours.** The final plugin builds memory entirely in the background: first open of a repo seeds the bank from commit history and surveys the structure; every session start, an idempotent engine tops it up — new commits (progressively deepening into full diffs, newest first), new sessions written back automatically. The five scattered integrations became one package with one runtime and a one-command installer:

```bash
npm install -g hindsight-coding-agents && hindsight-coding-agents install
```

## The final numbers

For the headline run we measured the *product*, not a hand-built index: the harness deletes the memory bank before each task and lets the plugin's own automatic pipeline rebuild it from empty, waiting on the plugin's public sync-status contract before the agent starts.

| Condition | Corrections / task | vs vanilla |
|---|---|---|
| Vanilla (no memory), mean of 3 runs | 0.97 | — |
| **Memory, out-of-box** (bank built from empty, automatically) | **0.67** | **−31%** |
| **Memory, matured banks** (history accrued from prior sessions) | **0.55** | **−44%** |

OpenCode + Gemini 3.5 Flash; 33/33 tasks solved in every memory run; injection verified on all 33; per-task cost *down* 39% ($0.79 → $0.48) because one targeted retrieval replaces a lot of exploration. In an earlier campaign with a stronger model (Claude Code + Sonnet, five runs per arm), the same mechanism cut corrections **58%** — the stronger the agent, the more its remaining failures are precisely the non-guessable decisions memory carries.

The fresh-vs-matured gap is the part we find most satisfying: 0.67 out of the box, 0.55 once the bank has lived with the project — because sessions write themselves back, **the agent gets better at your repo just by working in it**.

## Honest limitations

One suite, one codebase family. The out-of-box row is a single verified run (n=3 refresh in progress; the previous architecture's n=3 landed in the same range). And the dataset deliberately concentrates on tasks where memory *can* matter — it quantifies the last-mile-decision problem, not general coding ability. Dataset, harness, per-task results, and the full hardening journal — including the experiments where our own ideas lost — will be public at [agentmemorybenchmark.ai](https://agentmemorybenchmark.ai).

## Try it on the repo that knows things your agent doesn't

```bash
npm install -g hindsight-coding-agents && hindsight-coding-agents install
```

Point it at a [Hindsight server](https://vectorize.io/hindsight) in `~/.hindsight/coding-agent.json`, open a repo, and watch it introduce itself. Works with opencode, Claude Code, Codex CLI, Gemini CLI, and Cursor CLI — one plugin, one path. Docs: [coding agents integration](/sdks/integrations/coding-agents).
