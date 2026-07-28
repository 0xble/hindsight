/**
 * Harness-agnostic Hindsight HTTP client (raw fetch, no SDK dep).
 *
 * Every harness adapter and the backfill CLI go through this one client: it configures the bank
 * (missions + git/chat retain strategies), retains memories, reflects, drains async operations, and
 * creates knowledge pages. Nothing here knows about opencode/claude-code/etc.
 */
import { CODING_BANK_TEMPLATE } from "./missions";
import { sleep } from "./util";

export interface ClientOpts {
  apiUrl: string;
  apiToken?: string;
  bank: string;
  log?: (msg: string) => void;
}

export interface RetainOpts {
  timestamp?: string; // when the content occurred (temporal ranking)
  metadata?: Record<string, string>; // source provenance (returned with recalls)
  async?: boolean; // enqueue server-side (default) vs block on extraction
}

const TERMINAL = new Set(["completed", "failed", "cancelled", "error"]);

export class HindsightClient {
  readonly apiUrl: string;
  readonly apiToken?: string;
  readonly bank: string;
  readonly opIds: string[] = []; // async operation ids collected by retain(), for drain()
  private readonly log: (msg: string) => void;

  constructor(o: ClientOpts) {
    this.apiUrl = o.apiUrl.replace(/\/$/, "");
    this.apiToken = o.apiToken;
    this.bank = o.bank;
    this.log = o.log ?? (() => {});
  }

  private headers(): Record<string, string> {
    const h: Record<string, string> = { "Content-Type": "application/json" };
    if (this.apiToken) h["Authorization"] = `Bearer ${this.apiToken}`;
    return h;
  }

  bankUrl(suffix = ""): string {
    return `${this.apiUrl}/v1/default/banks/${encodeURIComponent(this.bank)}${suffix}`;
  }

  async req(method: string, url: string, body?: unknown): Promise<Response> {
    const r = await fetch(url, {
      method,
      headers: this.headers(),
      body: body ? JSON.stringify(body) : undefined,
    });
    if (!r.ok && r.status !== 404)
      throw new Error(`${method} ${url} -> ${r.status} ${await r.text()}`);
    return r;
  }

  /** Retain one memory. Async by default: enqueue extraction server-side and collect its op-id for drain(). */
  async retain(
    content: string,
    context: string,
    documentId: string,
    tags: string[],
    strategy: string,
    opts: RetainOpts = {}
  ): Promise<void> {
    const item: Record<string, unknown> = {
      content,
      context,
      document_id: documentId,
      tags,
      strategy,
    };
    if (opts.timestamp) item.timestamp = opts.timestamp;
    if (opts.metadata) item.metadata = opts.metadata;
    const isAsync = opts.async !== false;
    const r = await this.req("POST", this.bankUrl("/memories"), { items: [item], async: isAsync });
    if (isAsync) {
      try {
        const j = (await r.json()) as { operation_id?: string };
        if (j.operation_id) this.opIds.push(j.operation_id);
      } catch {
        /* ignore */
      }
    }
  }

  /**
   * Every document_id currently in the bank under a strategy tag (e.g. `source:git`), paginated into a
   * Set. Powers the incremental git-sync's "what's already ingested?" check — since git commits are stored
   * with document_id `git:<sha>`, the returned Set lets a caller diff a ref's commits against memory.
   */
  async listDocumentIds(tag: string): Promise<Set<string>> {
    const ids = new Set<string>();
    const limit = 500;
    for (let offset = 0; ; offset += limit) {
      const q = `?tags=${encodeURIComponent(tag)}&tags_match=all&limit=${limit}&offset=${offset}`;
      const r = await this.req("GET", this.bankUrl(`/documents${q}`));
      let items: { id?: string }[] = [];
      let total = 0;
      try {
        const j = (await r.json()) as { items?: { id?: string }[]; total?: number };
        items = j.items || [];
        total = j.total ?? 0;
      } catch {
        break;
      }
      for (const it of items) if (it.id) ids.add(it.id);
      if (items.length < limit || ids.size >= total) break; // last page reached
    }
    return ids;
  }

  /** Configure the bank in ONE SHOT: POST the coding bank template manifest to /import —
   *  missions, retain strategies, entity labels, AND the seeded knowledge pages (matched by
   *  stable id server-side, so re-applying every deepen run updates instead of duplicating).
   *  Creates the bank if missing. */
  async configureBank(opts: { reset?: boolean } = {}): Promise<void> {
    if (opts.reset) {
      await this.req("DELETE", this.bankUrl());
      this.log(`[bank] reset ${this.bank}`);
    }
    await this.req("POST", this.bankUrl("/import"), CODING_BANK_TEMPLATE);
    this.log(
      `[bank] template applied to ${this.bank}: missions, entity_labels {knowledge}, ` +
        `strategies {git, gitlog, conversation, document}, ${CODING_BANK_TEMPLATE.mental_models.length} knowledge pages`
    );
  }

  /** Count of operations still ACTIVE on this bank — the list includes terminal ops (completed/
   *  failed/cancelled), so filter by status. Powers syncStatus's "extractions drained" check. */
  async activeOperations(): Promise<number> {
    const r = await this.req("GET", this.bankUrl("/operations"));
    try {
      const j = (await r.json()) as {
        operations?: { status?: string }[];
        items?: { status?: string }[];
      };
      const ops = j.operations ?? j.items ?? [];
      return ops.filter((o) => !TERMINAL.has((o?.status || "").toLowerCase())).length;
    } catch {
      return 0;
    }
  }

  /** Poll each enqueued operation by id until terminal. LIST only shows active ops, so per-id GET is reliable. */
  async drain(ids: string[], label: string, maxMs = 60 * 60 * 1000): Promise<void> {
    if (!ids.length) return;
    this.log(`[wait] draining ${ids.length} ${label} operations …`);
    const start = Date.now();
    const pending = new Set(ids);
    let failed = 0;
    while (pending.size && Date.now() - start < maxMs) {
      await Promise.all(
        [...pending].map(async (id) => {
          try {
            const r = await fetch(this.bankUrl(`/operations/${id}`), { headers: this.headers() });
            if (!r.ok) return;
            const st = (((await r.json()) as { status?: string }).status || "").toLowerCase();
            if (TERMINAL.has(st)) {
              pending.delete(id);
              if (st !== "completed") failed++;
            }
          } catch {
            /* transient — retry next cycle */
          }
        })
      );
      if (pending.size) {
        this.log(`  … ${pending.size}/${ids.length} ${label} ops pending`);
        await sleep(5000);
      }
    }
    this.log(
      `[wait] ${label} drained — ${ids.length - pending.size} done, ${failed} failed` +
        (pending.size ? `, ${pending.size} still pending at timeout` : "")
    );
  }

  /** Reflect: synthesized, root-cause answer over the bank. Bounded so a slow server never hangs a caller. */
  async reflect(
    query: string,
    opts: { budget?: string; timeoutMs?: number } = {}
  ): Promise<string> {
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), opts.timeoutMs ?? 120000);
    try {
      const resp = await fetch(this.bankUrl("/reflect"), {
        method: "POST",
        headers: this.headers(),
        body: JSON.stringify({ query, budget: opts.budget ?? "high" }),
        signal: ctrl.signal,
      });
      if (!resp.ok) throw new Error(`reflect ${resp.status}`);
      const data = (await resp.json()) as { text?: string };
      return (data.text || "").trim();
    } finally {
      clearTimeout(timer);
    }
  }

  /**
   * List knowledge pages (ids + names only — no synthesized content).
   * detail=metadata (not the full/content default) keeps list payloads small: pages can carry
   * up to ~100KB of synthesized content each, which callers don't need just to enumerate them.
   */
  async listPages(): Promise<unknown> {
    const r = await this.req("GET", this.bankUrl("/mental-models?detail=metadata"));
    return await r.json();
  }

  /**
   * Read one knowledge page's synthesized content by id.
   * detail=content (not detail=full) — full additionally includes `reflect_response`, the
   * internal trace metadata used to build the page, which is 70-95% of the response bytes and
   * can blow past an MCP host's per-tool-result token cap.
   */
  async getPage(pageId: string): Promise<unknown> {
    const r = await this.req(
      "GET",
      this.bankUrl(`/mental-models/${encodeURIComponent(pageId)}?detail=content`)
    );
    if (r.status === 404) throw new Error(`knowledge page not found: ${pageId}`);
    return await r.json();
  }

  /** Hybrid (BM25 + vector, RRF-fused) server-side search over the bank's knowledge pages.
   *  Returns page-level hits with a relevance snippet — the real search behind
   *  hindsight_search_knowledge_pages. */
  async searchKnowledgePages(
    query: string,
    limit = 3
  ): Promise<{ id: string; name: string; snippet: string; score: number }[]> {
    const q = `?q=${encodeURIComponent(query)}&limit=${limit}`;
    const r = await this.req("GET", this.bankUrl(`/knowledge-base/search${q}`));
    const j = (await r.json()) as {
      results?: { id: string; name: string; snippet?: string; score?: number }[];
    };
    return (j.results ?? []).map((x) => ({
      id: x.id,
      name: x.name,
      snippet: x.snippet ?? "",
      score: x.score ?? 0,
    }));
  }

  /**
   * Create ONE custom knowledge page (used by seeding + captureInitiative; not exposed as an agent MCP tool).
   * NOT the same as `createPages()` below, which batch-synthesizes the fixed `PAGES` set at
   * backfill time from extracted facts — this creates a single page from a caller-chosen
   * `sourceQuery`, re-run on each consolidation.
   */
  async createPage(pageId: string, name: string, sourceQuery: string): Promise<unknown> {
    const r = await this.req("POST", this.bankUrl("/mental-models"), {
      id: pageId,
      name,
      source_query: sourceQuery,
      max_tokens: 4096,
      trigger: {
        mode: "delta",
        refresh_after_consolidation: true,
        fact_types: ["observation"],
        exclude_mental_models: true,
      },
    });
    return await r.json();
  }

  /** Update a page's name and/or source query. No-op (no request) if neither is provided. */
  async updatePage(
    pageId: string,
    updates: { name?: string; sourceQuery?: string }
  ): Promise<unknown> {
    const body: Record<string, string> = {};
    if (updates.name) body.name = updates.name;
    if (updates.sourceQuery) body.source_query = updates.sourceQuery;
    if (Object.keys(body).length === 0) {
      return { error: "Provide name or sourceQuery to update" };
    }
    const r = await this.req(
      "PATCH",
      this.bankUrl(`/mental-models/${encodeURIComponent(pageId)}`),
      body
    );
    if (r.status === 404) throw new Error(`knowledge page not found: ${pageId}`);
    return await r.json();
  }

  /** Permanently delete a knowledge page. */
  async deletePage(pageId: string): Promise<unknown> {
    const r = await this.req(
      "DELETE",
      this.bankUrl(`/mental-models/${encodeURIComponent(pageId)}`)
    );
    if (r.status === 404) throw new Error(`knowledge page not found: ${pageId}`);
    try {
      return await r.json();
    } catch {
      return { ok: true };
    }
  }

  /** URL/id-safe slug: lowercase, non-alphanumerics → "-", trim dashes, cap length; fallback "initiative". */
  private slugify(s: string): string {
    return (
      s
        .toLowerCase()
        .replace(/[^a-z0-9]+/g, "-")
        .replace(/^-+|-+$/g, "")
        .slice(0, 60) || "initiative"
    );
  }

  /**
   * Active-path capture: register a major feature as a per-initiative page + a tagged marker memory.
   * New initiative → creates a page under the Initiatives folder tagged `relatedPageId:<pageId>`.
   * Enhancement (relatesToPageId) → no new page; only a marker accruing to the existing page.
   */
  async captureInitiative(args: {
    title: string;
    summary: string;
    relatesToPageId?: string;
  }): Promise<{ page_id: string }> {
    // `/knowledge-base/pages` mints its OWN page id (kp-…); we can't set it. So for a new initiative
    // we create the page first and adopt the server-assigned id — that id is what the return value
    // and the marker's `relatedPageId` tag must use, or `read_knowledge_page(id)` 404s and the
    // `[[page:<id>]]` link points at nothing.
    let pageId = args.relatesToPageId;
    if (!pageId) {
      const folderId = await this.ensureFolder("Initiatives");
      const r = await this.req("POST", this.bankUrl("/knowledge-base/pages"), {
        name: args.title,
        source_query: `Summarize the "${args.title}" initiative: what is being built or changed and why, and its current state — drawn from the project's memory.`,
        parent_id: folderId,
        tags: ["knowledge:feature-work"],
        trigger: {
          fact_types: ["world", "experience", "observation"],
          refresh_after_consolidation: true,
        },
      });
      try {
        const j = (await r.json()) as { page_id?: string; id?: string };
        pageId = j.page_id ?? j.id;
      } catch {
        /* fall through to the slug fallback below */
      }
      pageId ||= `initiative-${this.slugify(args.title)}`; // last resort if the response lacked an id
    }
    const verb = args.relatesToPageId ? "Enhancement to an existing initiative" : "New initiative";
    const content = `${verb}: ${args.title}. ${args.summary}`;
    // Unique marker document id (NOT pageId) so repeated captures accrue instead of replacing.
    const markerId = `initiative-marker-${this.slugify(args.title)}-${Date.now()}`;
    await this.retain(
      content,
      "initiative marker",
      markerId,
      ["knowledge:feature-work", `relatedPageId:${pageId}`],
      "document",
      { async: true }
    );
    return { page_id: pageId };
  }

  /** Find a root folder by name (case-insensitive) or create it; returns its id. Fail-open to undefined. */
  async ensureFolder(name: string): Promise<string | undefined> {
    try {
      const tree = (await (await this.req("GET", this.bankUrl("/knowledge-base/tree"))).json()) as {
        roots?: { id?: string; kind?: string; name?: string }[];
      };
      const hit = (tree.roots || []).find(
        (n) => n.kind === "folder" && (n.name || "").toLowerCase() === name.toLowerCase()
      );
      if (hit?.id) return hit.id;
    } catch {
      /* fall through to create */
    }
    try {
      const r = await this.req("POST", this.bankUrl("/knowledge-base/folders"), { name });
      return ((await r.json()) as { id?: string }).id;
    } catch {
      return undefined;
    }
  }
}
