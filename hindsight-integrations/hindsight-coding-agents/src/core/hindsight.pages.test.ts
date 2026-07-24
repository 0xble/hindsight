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
});
