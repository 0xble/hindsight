import { afterEach, describe, expect, it, vi } from "vitest";
import { HindsightClient } from "./hindsight";

afterEach(() => vi.restoreAllMocks());

describe("HindsightClient.recall", () => {
  it("POSTs to /memories/recall and returns results", async () => {
    const calls: any[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string, init: any) => {
        calls.push({ url, body: JSON.parse(init.body) });
        return {
          ok: true,
          status: 200,
          json: async () => ({
            results: [{ text: "we use zod for validation", scores: { rerank: 0.9 } }],
          }),
        } as any;
      })
    );
    const c = new HindsightClient({ apiUrl: "http://x", bank: "repo-a" });
    const results = await c.recall("how do we validate input", { maxTokens: 512, budget: "mid" });
    expect(results).toHaveLength(1);
    expect(calls[0].url).toContain("/v1/default/banks/repo-a/memories/recall");
    expect(calls[0].body).toMatchObject({
      query: "how do we validate input",
      max_tokens: 512,
      budget: "mid",
    });
  });

  it("returns [] on a non-ok response instead of throwing", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => ({ ok: false, status: 500, text: async () => "err" }) as any)
    );
    const c = new HindsightClient({ apiUrl: "http://x", bank: "b" });
    expect(await c.recall("q")).toEqual([]);
  });
});
