import { afterEach, describe, expect, it, vi } from "vitest";
import { HindsightClient } from "./hindsight";

afterEach(() => vi.restoreAllMocks());

function stubFetch(calls: any[], jsonImpl: () => Promise<unknown> = async () => ({ ok: true })) {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init: any) => {
      calls.push({
        url,
        method: init?.method,
        body: init?.body ? JSON.parse(init.body) : undefined,
      });
      return { ok: true, status: 200, json: jsonImpl } as any;
    })
  );
}

describe("HindsightClient knowledge-page CRUD", () => {
  it("listPages GETs mental-models?detail=metadata", async () => {
    const calls: any[] = [];
    stubFetch(calls, async () => ({ pages: [] }));
    const c = new HindsightClient({ apiUrl: "http://x", bank: "repo-a" });
    const result = await c.listPages();
    expect(result).toEqual({ pages: [] });
    expect(calls).toHaveLength(1);
    expect(calls[0].method).toBe("GET");
    expect(calls[0].url).toContain("/v1/default/banks/repo-a/mental-models?detail=metadata");
  });

  it("getPage GETs mental-models/{id}?detail=content", async () => {
    const calls: any[] = [];
    stubFetch(calls, async () => ({ id: "p1" }));
    const c = new HindsightClient({ apiUrl: "http://x", bank: "repo-a" });
    const result = await c.getPage("p1");
    expect(result).toEqual({ id: "p1" });
    expect(calls[0].method).toBe("GET");
    expect(calls[0].url).toContain("/mental-models/p1?detail=content");
  });

  it("createPage POSTs to mental-models with the exact payload shape", async () => {
    const calls: any[] = [];
    stubFetch(calls, async () => ({ page_id: "p1" }));
    const c = new HindsightClient({ apiUrl: "http://x", bank: "repo-a" });
    const result = await c.createPage("p1", "My Page", "how do we X?");
    expect(result).toEqual({ page_id: "p1" });
    expect(calls[0].method).toBe("POST");
    expect(calls[0].url.endsWith("/mental-models")).toBe(true);
    expect(calls[0].body).toEqual({
      id: "p1",
      name: "My Page",
      source_query: "how do we X?",
      max_tokens: 4096,
      trigger: {
        mode: "delta",
        refresh_after_consolidation: true,
        fact_types: ["observation"],
        exclude_mental_models: true,
      },
    });
  });

  it("updatePage PATCHes only the provided fields (name only)", async () => {
    const calls: any[] = [];
    stubFetch(calls, async () => ({ id: "p1" }));
    const c = new HindsightClient({ apiUrl: "http://x", bank: "repo-a" });
    const result = await c.updatePage("p1", { name: "New" });
    expect(result).toEqual({ id: "p1" });
    expect(calls[0].method).toBe("PATCH");
    expect(calls[0].url).toContain("/mental-models/p1");
    expect(calls[0].body).toEqual({ name: "New" });
    expect(calls[0].body).not.toHaveProperty("source_query");
  });

  it("updatePage PATCHes only the provided fields (sourceQuery only)", async () => {
    const calls: any[] = [];
    stubFetch(calls, async () => ({ id: "p1" }));
    const c = new HindsightClient({ apiUrl: "http://x", bank: "repo-a" });
    const result = await c.updatePage("p1", { sourceQuery: "new query?" });
    expect(result).toEqual({ id: "p1" });
    expect(calls[0].body).toEqual({ source_query: "new query?" });
    expect(calls[0].body).not.toHaveProperty("name");
  });

  it("updatePage with neither field short-circuits without calling fetch", async () => {
    const calls: any[] = [];
    stubFetch(calls);
    const fetchSpy = vi.fn();
    vi.stubGlobal("fetch", fetchSpy);
    const c = new HindsightClient({ apiUrl: "http://x", bank: "repo-a" });
    const result = await c.updatePage("p1", {});
    expect(result).toEqual({ error: "Provide name or sourceQuery to update" });
    expect(fetchSpy).not.toHaveBeenCalled();
  });

  it("deletePage DELETEs mental-models/{id}", async () => {
    const calls: any[] = [];
    stubFetch(calls, async () => ({ deleted: true }));
    const c = new HindsightClient({ apiUrl: "http://x", bank: "repo-a" });
    const result = await c.deletePage("p1");
    expect(result).toEqual({ deleted: true });
    expect(calls[0].method).toBe("DELETE");
    expect(calls[0].url).toContain("/mental-models/p1");
  });

  it("deletePage falls back to { ok: true } when the response body isn't JSON", async () => {
    const calls: any[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string, init: any) => {
        calls.push({ url, method: init?.method });
        return {
          ok: true,
          status: 200,
          json: async () => {
            throw new Error("Unexpected end of JSON input");
          },
        } as any;
      })
    );
    const c = new HindsightClient({ apiUrl: "http://x", bank: "repo-a" });
    const result = await c.deletePage("p1");
    expect(result).toEqual({ ok: true });
  });

  it("URL-encodes pageId in getPage/updatePage/deletePage suffixes", async () => {
    const calls: any[] = [];
    stubFetch(calls, async () => ({ ok: true }));
    const c = new HindsightClient({ apiUrl: "http://x", bank: "repo-a" });
    await c.getPage("p 1/x");
    expect(calls[0].url).toContain(`/mental-models/${encodeURIComponent("p 1/x")}?detail=content`);
  });

  function stub404() {
    vi.stubGlobal(
      "fetch",
      vi.fn(
        async () => ({ ok: false, status: 404, json: async () => ({ detail: "not found" }) }) as any
      )
    );
  }

  it("getPage throws on 404 instead of returning the error envelope", async () => {
    stub404();
    const c = new HindsightClient({ apiUrl: "http://x", bank: "repo-a" });
    await expect(c.getPage("missing")).rejects.toThrow("knowledge page not found: missing");
  });

  it("updatePage throws on 404 instead of returning the error envelope", async () => {
    stub404();
    const c = new HindsightClient({ apiUrl: "http://x", bank: "repo-a" });
    await expect(c.updatePage("missing", { name: "New" })).rejects.toThrow(
      "knowledge page not found: missing"
    );
  });

  it("deletePage throws on 404 instead of returning { ok: true }", async () => {
    stub404();
    const c = new HindsightClient({ apiUrl: "http://x", bank: "repo-a" });
    await expect(c.deletePage("missing")).rejects.toThrow("knowledge page not found: missing");
  });
});

/** Route JSON responses by (method, url-substring) so multi-call flows can return distinct bodies. */
function stubFetchRouted(
  calls: any[],
  routes: { match: (method: string, url: string) => boolean; json: unknown }[]
) {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init: any) => {
      const method = init?.method;
      calls.push({ url, method, body: init?.body ? JSON.parse(init.body) : undefined });
      const route = routes.find((r) => r.match(method, url));
      return { ok: true, status: 200, json: async () => route?.json ?? { ok: true } } as any;
    })
  );
}

describe("HindsightClient.createPages Initiatives folder (unscoped pages, no tags)", () => {
  it("each seeded page POST is UNSCOPED: name + source_query + trigger, NO tags field", async () => {
    const calls: any[] = [];
    stubFetch(calls, async () => ({})); // no operation_id -> drain no-ops
    const c = new HindsightClient({ apiUrl: "http://x", bank: "repo-a" });
    await c.createPages();

    const pagePosts = calls.filter(
      (k) => k.method === "POST" && k.url.endsWith("/knowledge-base/pages")
    );
    expect(pagePosts.length).toBe(5);
    for (const post of pagePosts) {
      expect(typeof post.body.name).toBe("string");
      expect(typeof post.body.source_query).toBe("string");
      expect(post.body.trigger).toEqual({
        fact_types: ["world", "experience", "observation"],
        refresh_after_consolidation: true,
      });
      expect(post.body).not.toHaveProperty("tags");
    }
  });

  it("parents ONLY the Initiatives page under the folder id returned by POST /knowledge-base/folders", async () => {
    const calls: any[] = [];
    stubFetchRouted(calls, [
      { match: (m, u) => m === "GET" && u.endsWith("/knowledge-base/tree"), json: { roots: [] } },
      {
        match: (m, u) => m === "POST" && u.endsWith("/knowledge-base/folders"),
        json: { id: "folder-123" },
      },
      {
        match: (m, u) => m === "POST" && u.endsWith("/knowledge-base/pages"),
        json: { page_id: "p" },
      },
    ]);
    const c = new HindsightClient({ apiUrl: "http://x", bank: "repo-a" });
    await c.createPages();

    const pagePosts = calls.filter(
      (k) => k.method === "POST" && k.url.endsWith("/knowledge-base/pages")
    );
    const initiatives = pagePosts.find((k) => k.body.name === "Initiatives and enhancements");
    expect(initiatives.body.parent_id).toBe("folder-123");
    for (const post of pagePosts) {
      if (post.body.name !== "Initiatives and enhancements") {
        expect(post.body.parent_id).toBeUndefined();
      }
    }
  });
});

describe("HindsightClient.ensureFolder", () => {
  it("returns an existing root folder's id (case-insensitive) and does NOT POST a duplicate", async () => {
    const calls: any[] = [];
    stubFetchRouted(calls, [
      {
        match: (m, u) => m === "GET" && u.endsWith("/knowledge-base/tree"),
        json: { roots: [{ id: "existing-1", kind: "folder", name: "initiatives" }] },
      },
    ]);
    const c = new HindsightClient({ apiUrl: "http://x", bank: "repo-a" });
    const id = await c.ensureFolder("Initiatives");
    expect(id).toBe("existing-1");
    expect(
      calls.some((k) => k.method === "POST" && k.url.endsWith("/knowledge-base/folders"))
    ).toBe(false);
  });

  it("creates the folder and returns its new id when the tree has no match", async () => {
    const calls: any[] = [];
    stubFetchRouted(calls, [
      { match: (m, u) => m === "GET" && u.endsWith("/knowledge-base/tree"), json: { roots: [] } },
      {
        match: (m, u) => m === "POST" && u.endsWith("/knowledge-base/folders"),
        json: { id: "new-folder" },
      },
    ]);
    const c = new HindsightClient({ apiUrl: "http://x", bank: "repo-a" });
    const id = await c.ensureFolder("Initiatives");
    expect(id).toBe("new-folder");
    const post = calls.find(
      (k) => k.method === "POST" && k.url.endsWith("/knowledge-base/folders")
    );
    expect(post.body).toEqual({ name: "Initiatives" });
  });
});

describe("HindsightClient.captureInitiative", () => {
  it("new initiative: POSTs a per-initiative page + a marker retain sharing the same relatedPageId", async () => {
    const calls: any[] = [];
    stubFetchRouted(calls, [
      { match: (m, u) => m === "GET" && u.endsWith("/knowledge-base/tree"), json: { roots: [] } },
      {
        match: (m, u) => m === "POST" && u.endsWith("/knowledge-base/folders"),
        json: { id: "folder-abc" },
      },
      {
        match: (m, u) => m === "POST" && u.endsWith("/knowledge-base/pages"),
        json: { page_id: "pg" },
      },
      {
        match: (m, u) => m === "POST" && u.endsWith("/memories"),
        json: { operation_id: "op-1" },
      },
    ]);
    const c = new HindsightClient({ apiUrl: "http://x", bank: "repo-a" });
    const result = await c.captureInitiative({
      title: "Retry backoff for the uploader",
      summary: "Add exponential backoff so transient upload failures retry.",
    });

    // The returned id is the SERVER-ASSIGNED page id ("pg" from the mock), NOT the derived slug —
    // the /knowledge-base/pages endpoint mints its own id.
    expect(result).toEqual({ page_id: "pg" });

    // Page POST: name = title, nested under the Initiatives folder. The page itself carries NO
    // tags field at all (no tag taxonomy to maintain).
    const pagePost = calls.find(
      (k) => k.method === "POST" && k.url.endsWith("/knowledge-base/pages")
    );
    expect(pagePost).toBeDefined();
    expect(pagePost.body.name).toBe("Retry backoff for the uploader");
    expect(pagePost.body.parent_id).toBe("folder-abc");
    expect(pagePost.body).not.toHaveProperty("tags");

    // Marker retain POST to /memories: the ONLY tag is relatedPageId, pointing at the REAL
    // server-assigned page id ("pg").
    const memPost = calls.find((k) => k.method === "POST" && k.url.endsWith("/memories"));
    expect(memPost).toBeDefined();
    const item = memPost.body.items[0];
    expect(item.tags).toEqual(["knowledge:feature-work", "relatedPageId:pg"]);
    expect(item.strategy).toBe("document");
    expect(memPost.body.async).toBe(true);
    // Unique per-marker document id (NOT the page id) so repeated captures accrue.
    expect(item.document_id).not.toBe("pg");
    expect(item.document_id).toContain("initiative-marker-retry-backoff-for-the-uploader-");

    // The returned page id and the marker tag's id must be identical (the real page node id).
    const tagId = item.tags
      .find((t: string) => t.startsWith("relatedPageId:"))
      .slice("relatedPageId:".length);
    expect(tagId).toBe(result.page_id);
  });

  it("enhancement (relatesToPageId): NO page POST; marker tagged the existing page id", async () => {
    const calls: any[] = [];
    stubFetchRouted(calls, [
      {
        match: (m, u) => m === "POST" && u.endsWith("/memories"),
        json: { operation_id: "op-1" },
      },
    ]);
    const c = new HindsightClient({ apiUrl: "http://x", bank: "repo-a" });
    const result = await c.captureInitiative({
      title: "More backoff tuning",
      summary: "Tweak the jitter window.",
      relatesToPageId: "initiative-x",
    });

    expect(result).toEqual({ page_id: "initiative-x" });

    // No new page created for enhancements.
    expect(calls.some((k) => k.method === "POST" && k.url.endsWith("/knowledge-base/pages"))).toBe(
      false
    );
    // No folder lookup/creation either.
    expect(calls.some((k) => k.method === "GET" && k.url.endsWith("/knowledge-base/tree"))).toBe(
      false
    );

    const memPost = calls.find((k) => k.method === "POST" && k.url.endsWith("/memories"));
    expect(memPost).toBeDefined();
    const item = memPost.body.items[0];
    expect(item.tags).toEqual(["knowledge:feature-work", "relatedPageId:initiative-x"]);
    expect(item.content).toContain("Enhancement to an existing initiative");
  });
});

describe("HindsightClient.configureBank config PATCH", () => {
  it("PATCHes /config with the retain strategies + the knowledge entity_labels tier", async () => {
    const calls: any[] = [];
    stubFetch(calls, async () => ({ ok: true }));
    const c = new HindsightClient({ apiUrl: "http://x", bank: "repo-a" });
    await c.configureBank();

    const configCall = calls.find((c) => c.method === "PATCH" && c.url.endsWith("/config"));
    expect(configCall).toBeDefined();
    const updates = configCall.body.updates;

    expect(updates.entity_labels).toEqual([
      expect.objectContaining({ key: "knowledge", tag: true, optional: true }),
    ]);
    expect(updates.entities_allow_free_form).toBe(true);
    expect(updates.retain_default_strategy).toBe("git");
    expect(Object.keys(updates.retain_strategies)).toEqual(
      expect.arrayContaining(["git", "gitlog", "conversation", "document"])
    );
  });
});
