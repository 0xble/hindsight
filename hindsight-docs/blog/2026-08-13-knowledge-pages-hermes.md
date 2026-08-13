---
title: "Turn Your Hermes Agent's Memory Into a Living Knowledge Base"
authors: [benfrank241]
slug: "2026/08/13/knowledge-pages-hermes-agent"
date: 2026-08-13T12:00
tags: [hindsight, knowledge-pages, hermes, agent-memory, mental-models, self-hosting]
description: "Knowledge Pages are living documents a memory bank writes about itself. Point them at the same bank your Hermes agent uses and you get a self-healing wiki of everything it has learned."
image: /img/blog/knowledge-pages-hermes.png
hide_table_of_contents: true
---

![Knowledge Pages: a self-healing wiki projected from the same Hindsight bank your Hermes agent reads and writes](/img/blog/knowledge-pages-hermes.png)

A memory bank accumulates thousands of facts, and you cannot read any of them. Your agent recalls the right one at the right moment, which is the point, but there is no page you can open to see what it actually knows about your project. [Knowledge Pages](https://hindsight.vectorize.io/developer/knowledge-pages), shipped in Hindsight 0.9.0, close that gap: they are living documents a bank writes about itself, and they rewrite themselves as the bank learns.

That matters most when an agent is doing the learning for you. If you run [Hermes](https://hindsight.vectorize.io/sdks/integrations/hermes) with Hindsight as its memory provider, every session quietly deposits what it learned into a bank. Point Knowledge Pages at that same bank and the agent's accumulated memory becomes a wiki you can read, a graph you can explore, and a knowledge base you can trust to stay current.

<!-- truncate -->

## TL;DR

- **Knowledge Pages** are self-maintaining documents synthesized from a bank's memory. Each page answers one question ("What's our error-handling convention?") and heals itself as new memory lands.
- Under the hood, a page *is* a [mental model](https://hindsight.vectorize.io/developer/mental-models) with the hard decisions pre-made: built from consolidated **observations**, refreshed incrementally, never citing other pages.
- You do not write a page's body. You give it a name and a question; Hindsight synthesizes the content and keeps it current after every consolidation.
- **Hermes** uses Hindsight as a memory provider, auto-recalling before each turn and auto-retaining after. Those retentions become the observations your pages are built from.
- Put both on the **same bank** and you get a readable, self-healing view of what your agent knows. Honest caveat: today you create and browse pages out-of-band (CLI, dashboard, or `hindsight fs mount`), not through Hermes's own tools.
- The Control Plane gives you three windows onto that bank: a **browsable page tree**, **rendered pages** that refresh themselves, an **editor** where a question defines a page, and a **memory constellation** that graphs the whole store.

## What a Knowledge Page actually is

Start with the shape and the engine, because they are different things. The shape is a wiki: pages organized in folders, browsable, searchable, and projectable to disk as ordinary markdown. The engine underneath is memory.

A page is a **projected view** over processed memory, the way a database view is not a table. Before a page is written, Hindsight has already extracted facts from raw sessions and documents, deduplicated them, and reconciled their contradictions through consolidation. Your raw history stays the source of truth about *what was said*. The page is the reconciled truth about *what holds* right now.

Concretely, a Knowledge Page is a mental model with a set of defaults chosen for you:

- It is built from the bank's **observations** — the consolidated, deduplicated beliefs — rather than raw conversational detail.
- It refreshes **incrementally**, editing the document when consolidation produces new knowledge in its scope instead of regenerating it, so hand-tuned structure survives.
- It **never reads other pages**, so pages cannot cite each other into a feedback loop.
- It gets a larger content budget, because it is a document rather than a one-line answer.

You supply a name and a question. Everything else is a default you can override.

## The Knowledge view: your agent's memory as a browsable wiki

Here is what that looks like in the Control Plane. Every page a bank has written sits in a searchable tree, grouped into folders, with a green dot when a page is freshly built and a **Mental Models** tab alongside the pages.

![The Knowledge view: acme-api's pages organized in a folder tree — Architecture, Conventions, Decisions, Open initiatives — each synthesized from the bank's memory and updated automatically](/img/blog/knowledge-pages-tree.png)

**Why it matters for Hermes users:** everything your Hermes agent picks up across sessions becomes navigable documentation. Instead of trusting an opaque memory store, you open Architecture, Conventions, or Decisions and read what the agent actually believes about your project, in the language your team already uses.

## Why it beats a hand-written wiki

Every team has tried the wiki. It is accurate for a week. Then a decision changes, nobody updates the page, and by month two the wiki is a beautifully formatted lie. The failure mode is not laziness. It is that the wiki is *storage*, and storage has to be maintained by hand.

A Knowledge Page is not storage. It is a rendering of memory, so it heals itself rather than rotting. When a team decides X and later amends it to Y, the page says Y, and can say why, instead of preserving both a paragraph apart. Delete a page and nothing is lost: it re-projects from memory on the next build. That inversion, from a document you maintain to a document that maintains itself, is the whole idea.

![A rendered Knowledge Page — "System architecture" — synthesized from the bank's observations into clean prose and marked updated moments after the latest consolidation](/img/blog/knowledge-pages-rendered.png)

A finished page reads like something a careful teammate wrote, except nobody did. The "updated" timestamp is the tell: it refreshed itself the moment new memory landed. **For Hermes users**, that means the page describing your system stays honest as the agent keeps learning, with zero upkeep, and there is an Edit button when you want to steer it.

## Creating a page: a name and a question

You never author the body. You describe what the page should answer, and Hindsight synthesizes it from the bank's observations. Creation is asynchronous: the page is stored with placeholder content and the first build runs in the background.

![The page editor: a Name field and a "Source query" — the question that rebuilds this page from memory — plus optional tags, where a type: tag sets the page's type](/img/blog/knowledge-pages-edit.png)

The whole contract lives in that dialog. The **source query** is the question the system re-asks after every consolidation to rebuild the page; change the question and the content re-synthesizes from your memories. Tags organize pages, and a `type:` tag classifies them. You are curating by asking a better question, not by writing and maintaining prose. **For Hermes users**, that is the difference between owning a wiki and owning a list of questions you want your agent's memory to keep answering.

The fastest path is the CLI:

```bash
# create a folder and a page inside it
hindsight knowledge-base create-folder --bank my-project --name "Architecture"
hindsight knowledge-base create-page \
  --bank my-project \
  --name "Error-handling convention" \
  --source-query "How does this project handle and surface errors?"
```

The same operations are available over the [HTTP API](https://hindsight.vectorize.io/developer/api/knowledge-pages), in the Control Plane dashboard's Knowledge view, and as a live filesystem you can mount:

```bash
hindsight fs mount --bank my-project ./knowledge
```

That last one projects the whole tree to disk as markdown files, so your agent's knowledge is browsable, greppable, and easy to drop into version control.

## See the whole memory: the bank at a glance

Pages are one view of a bank. The bank's Home gives you the other: a **memory constellation** that plots every memory and the links between them, colored by how they connect — semantic, temporal, entity, and causal — sitting right next to the Knowledge pages panel and the raw documents that fed them.

![The acme-api bank overview: a memory constellation of 30 memories and 604 links colored by connection type, beside the Knowledge pages panel and recent documents](/img/blog/knowledge-pages-overview.png)

This is the same memory your Hermes agent recalls and reflects over, made visible. **Why it matters for Hermes users:** you can see the shape of what your agent knows — how dense a topic is, what connects to what, which documents seeded it — and confirm recall has real material to draw on instead of taking it on faith. When an answer looks thin, the constellation tells you whether the memory is missing or just unretrieved.

## Using Knowledge Pages with a Hermes agent

[Hermes](https://github.com/NousResearch/hermes-agent), Nous Research's open, model-agnostic agent, treats memory as a pluggable provider. Select Hindsight and two things happen automatically: before each turn it recalls relevant memory and injects it into the system prompt, and after each turn it retains the exchange. You configure it once:

```bash
hermes memory setup        # choose "hindsight"
hermes memory status       # confirm the provider and bank
```

The provider config lives at `~/.hermes/hindsight/config.json`. The key that ties everything together is `bank_id` (it defaults to `hermes`, and you should set one bank per project or repo). That bank is the join point.

Here is the workflow, and the honest boundary that comes with it. Hermes's auto-retain feeds the bank; consolidation turns those retentions into observations. Knowledge Pages, pointed at the **same bank**, synthesize over exactly those observations and heal themselves after each consolidation. So the loop is:

1. Hermes works and learns. Its retentions become observations in `bank_id`.
2. You curate a handful of pages over that bank — Architecture, Conventions, Key Decisions, Open Questions — via the CLI, dashboard, or a mounted filesystem.
3. Every time the bank consolidates, the pages re-project from the newest memory. The wiki tracks the agent without anyone editing it.

The boundary worth stating plainly: Hermes's own tools are `recall`, `retain`, and `reflect` over individual memories. It does not read or write Knowledge Pages as documents today, so you create and browse them out-of-band. What connects them is the shared bank, not a Hermes page tool. It is a real distinction, and pretending otherwise would set the wrong expectation.

What Hermes *does* get from the same memory is [`reflect`](https://hindsight.vectorize.io/blog/2026/07/24/recall-vs-reflect): synthesized reasoning over everything the bank holds, the same reconciled memory your pages are built from. Pages give a human a readable document; `reflect` gives the agent a reasoned answer. Both draw on one store.

## Why the pairing is worth it

**A readable audit of what your agent knows.** An agent that silently accumulates memory is hard to trust and harder to debug. A folder of self-healing pages turns that opaque bank into something a teammate can open, review, and correct, without freezing the memory the agent depends on.

**Curated context that does not drift from the live agent.** Because pages re-project from the same bank Hermes reads and writes, your Architecture page cannot quietly disagree with what the agent actually believes. Update the memory and the page follows.

**A fully-open, local stack.** This is where the Hermes crowd tends to care most. Hermes is MIT-licensed and talks to any OpenAI-compatible endpoint, [self-hosted Hindsight](https://hindsight.vectorize.io/blog/2026/07/17/hermes-hindsight-open-stack) is one Docker command with embedded PostgreSQL and local embeddings, and an open-weights model like `gpt-oss-20b` closes the loop. In that configuration the memory layer, pages included, makes no external network calls. Your agent's wiki lives entirely on your machine.

## Quick start

1. Stand up a bank. The fast path is [Hindsight Cloud](https://ui.hindsight.vectorize.io/signup); the private path is a self-hosted server with `HINDSIGHT_MODE=local`.
2. Point Hermes at it: `hermes memory setup`, choose Hindsight, and set `bank_id` to something project-specific.
3. Let it run. A few sessions in, the bank has real observations to synthesize from.
4. Create your first pages against that bank with `hindsight knowledge-base create-page`, or mount the tree with `hindsight fs mount` and read them as markdown.
5. Keep working. The pages heal themselves as the agent learns.

Memory is what makes an agent worth returning to. Knowledge Pages make that memory legible, and pairing them with an agent that fills the bank for you means the documentation writes and repairs itself. Read the [launch notes](https://hindsight.vectorize.io/blog/2026/08/06/hindsight-0-9-0), or start with a free [Hindsight Cloud](https://ui.hindsight.vectorize.io/signup) bank and give your Hermes agent a wiki it can never let go stale.

---

**Learn more:**
- [Knowledge Pages developer guide](https://hindsight.vectorize.io/developer/knowledge-pages) — the concept and the model underneath
- [Hermes integration](https://hindsight.vectorize.io/sdks/integrations/hermes) — provider setup, memory modes, and config
- [The fully-open Hermes + Hindsight stack](https://hindsight.vectorize.io/blog/2026/07/17/hermes-hindsight-open-stack) — self-hosting the whole thing
