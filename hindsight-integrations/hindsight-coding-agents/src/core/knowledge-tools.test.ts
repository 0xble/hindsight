import { describe, expect, it, vi } from "vitest";
import { buildKnowledgeTools } from "./knowledge-tools";
import type { HindsightClient } from "./hindsight";

/** Minimal stub of the HindsightClient surface the tools call — no SDK, no network. */
function stubClient(overrides: Partial<Record<string, ReturnType<typeof vi.fn>>> = {}) {
  return {
    listPages: vi.fn(async () => ({ pages: [] })),
    getPage: vi.fn(async (_id: string) => ({ id: _id })),
    createPage: vi.fn(async (_id: string, _name: string, _q: string) => ({ page_id: _id })),
    updatePage: vi.fn(async (_id: string, _u: unknown) => ({ id: _id })),
    deletePage: vi.fn(async (_id: string) => ({ ok: true })),
    recall: vi.fn(async (_q: string, _o: unknown) => [{ text: "a fact" }]),
    retain: vi.fn(async (..._args: unknown[]) => undefined),
    ...overrides,
  } as unknown as HindsightClient;
}

function findTool(tools: ReturnType<typeof buildKnowledgeTools>, name: string) {
  const t = tools.find((t) => t.name === name);
  if (!t) throw new Error(`tool not found: ${name}`);
  return t;
}

describe("buildKnowledgeTools", () => {
  it("agent_knowledge_get_current_bank returns {bank_id} without touching the client", async () => {
    const client = stubClient();
    const tools = buildKnowledgeTools(client, "repo-a");
    const tool = findTool(tools, "agent_knowledge_get_current_bank");
    const result = await tool.handler({});
    expect(result.isError).toBeFalsy();
    expect(JSON.parse(result.content[0].text)).toEqual({ bank_id: "repo-a" });
    expect(client.listPages).not.toHaveBeenCalled();
    expect(client.getPage).not.toHaveBeenCalled();
    expect(client.recall).not.toHaveBeenCalled();
  });

  it("agent_knowledge_list_pages calls client.listPages() with no args", async () => {
    const client = stubClient({ listPages: vi.fn(async () => ({ pages: [{ id: "p1" }] })) });
    const tools = buildKnowledgeTools(client, "repo-a");
    const tool = findTool(tools, "agent_knowledge_list_pages");
    const result = await tool.handler({});
    expect(client.listPages).toHaveBeenCalledWith();
    expect(JSON.parse(result.content[0].text)).toEqual({ pages: [{ id: "p1" }] });
  });

  it("agent_knowledge_get_page calls client.getPage(page_id)", async () => {
    const client = stubClient({ getPage: vi.fn(async (id: string) => ({ id, name: "X" })) });
    const tools = buildKnowledgeTools(client, "repo-a");
    const tool = findTool(tools, "agent_knowledge_get_page");
    const result = await tool.handler({ page_id: "p1" });
    expect(client.getPage).toHaveBeenCalledWith("p1");
    expect(JSON.parse(result.content[0].text)).toEqual({ id: "p1", name: "X" });
  });

  it("agent_knowledge_create_page calls client.createPage(page_id, name, source_query)", async () => {
    const client = stubClient();
    const tools = buildKnowledgeTools(client, "repo-a");
    const tool = findTool(tools, "agent_knowledge_create_page");
    const result = await tool.handler({
      page_id: "p1",
      name: "My Page",
      source_query: "how do we X?",
    });
    expect(client.createPage).toHaveBeenCalledWith("p1", "My Page", "how do we X?");
    expect(JSON.parse(result.content[0].text)).toEqual({ page_id: "p1" });
  });

  it("agent_knowledge_update_page calls client.updatePage(page_id, {name, sourceQuery})", async () => {
    const client = stubClient();
    const tools = buildKnowledgeTools(client, "repo-a");
    const tool = findTool(tools, "agent_knowledge_update_page");
    const result = await tool.handler({ page_id: "p1", name: "New", source_query: "new q?" });
    expect(client.updatePage).toHaveBeenCalledWith("p1", { name: "New", sourceQuery: "new q?" });
    expect(JSON.parse(result.content[0].text)).toEqual({ id: "p1" });
  });

  it("agent_knowledge_update_page passes undefined through for omitted optional fields", async () => {
    const client = stubClient();
    const tools = buildKnowledgeTools(client, "repo-a");
    const tool = findTool(tools, "agent_knowledge_update_page");
    await tool.handler({ page_id: "p1" });
    expect(client.updatePage).toHaveBeenCalledWith("p1", {
      name: undefined,
      sourceQuery: undefined,
    });
  });

  it("agent_knowledge_delete_page calls client.deletePage(page_id)", async () => {
    const client = stubClient();
    const tools = buildKnowledgeTools(client, "repo-a");
    const tool = findTool(tools, "agent_knowledge_delete_page");
    const result = await tool.handler({ page_id: "p1" });
    expect(client.deletePage).toHaveBeenCalledWith("p1");
    expect(JSON.parse(result.content[0].text)).toEqual({ ok: true });
  });

  it("agent_knowledge_recall calls client.recall(query, {maxTokens: max_tokens})", async () => {
    const client = stubClient();
    const tools = buildKnowledgeTools(client, "repo-a");
    const tool = findTool(tools, "agent_knowledge_recall");
    const result = await tool.handler({ query: "how do we validate?", max_tokens: 512 });
    expect(client.recall).toHaveBeenCalledWith("how do we validate?", { maxTokens: 512 });
    expect(JSON.parse(result.content[0].text)).toEqual([{ text: "a fact" }]);
  });

  it("agent_knowledge_recall passes maxTokens: undefined when max_tokens omitted", async () => {
    const client = stubClient();
    const tools = buildKnowledgeTools(client, "repo-a");
    const tool = findTool(tools, "agent_knowledge_recall");
    await tool.handler({ query: "q" });
    expect(client.recall).toHaveBeenCalledWith("q", { maxTokens: undefined });
  });

  it("agent_knowledge_ingest slugifies the title and calls client.retain(...) with the 'document' strategy", async () => {
    const client = stubClient();
    const tools = buildKnowledgeTools(client, "repo-a");
    const tool = findTool(tools, "agent_knowledge_ingest");
    const result = await tool.handler({ title: "My Title", content: "some content" });
    expect(client.retain).toHaveBeenCalledWith(
      "some content",
      "ingested document",
      "my-title",
      ["source:upload"],
      "document",
      { async: true }
    );
    expect(JSON.parse(result.content[0].text)).toEqual({ ok: true, doc_id: "my-title" });
  });

  it("agent_knowledge_ingest collapses internal whitespace runs in the title into single hyphens", async () => {
    const client = stubClient();
    const tools = buildKnowledgeTools(client, "repo-a");
    const tool = findTool(tools, "agent_knowledge_ingest");
    await tool.handler({ title: "Repo   Core  Concepts", content: "x" });
    expect(client.retain).toHaveBeenCalledWith(
      "x",
      "ingested document",
      "repo-core-concepts",
      ["source:upload"],
      "document",
      { async: true }
    );
  });

  it("agent_knowledge_ingest strips punctuation from the title into a safe slug", async () => {
    const client = stubClient();
    const tools = buildKnowledgeTools(client, "repo-a");
    const tool = findTool(tools, "agent_knowledge_ingest");
    await tool.handler({ title: "Repo: Component Map! (v2/final)", content: "x" });
    expect(client.retain).toHaveBeenCalledWith(
      "x",
      "ingested document",
      "repo-component-map-v2-final",
      ["source:upload"],
      "document",
      { async: true }
    );
  });

  it("agent_knowledge_ingest falls back to 'doc' when the title has no safe characters", async () => {
    const client = stubClient();
    const tools = buildKnowledgeTools(client, "repo-a");
    const tool = findTool(tools, "agent_knowledge_ingest");
    await tool.handler({ title: "!!!///???", content: "x" });
    expect(client.retain).toHaveBeenCalledWith(
      "x",
      "ingested document",
      "doc",
      ["source:upload"],
      "document",
      { async: true }
    );
  });

  for (const name of [
    "agent_knowledge_list_pages",
    "agent_knowledge_get_page",
    "agent_knowledge_create_page",
    "agent_knowledge_update_page",
    "agent_knowledge_delete_page",
    "agent_knowledge_recall",
    "agent_knowledge_ingest",
  ] as const) {
    it(`${name} returns isError:true with the error text when the client method throws`, async () => {
      const boom = new Error("boom: not found");
      const client = stubClient({
        listPages: vi.fn(async () => {
          throw boom;
        }),
        getPage: vi.fn(async () => {
          throw boom;
        }),
        createPage: vi.fn(async () => {
          throw boom;
        }),
        updatePage: vi.fn(async () => {
          throw boom;
        }),
        deletePage: vi.fn(async () => {
          throw boom;
        }),
        recall: vi.fn(async () => {
          throw boom;
        }),
        retain: vi.fn(async () => {
          throw boom;
        }),
      });
      const tools = buildKnowledgeTools(client, "repo-a");
      const tool = findTool(tools, name);
      const args =
        name === "agent_knowledge_create_page"
          ? { page_id: "p1", name: "N", source_query: "q?" }
          : name === "agent_knowledge_recall"
            ? { query: "q" }
            : name === "agent_knowledge_ingest"
              ? { title: "T", content: "C" }
              : { page_id: "p1" };
      const result = await tool.handler(args);
      expect(result.isError).toBe(true);
      expect(JSON.parse(result.content[0].text)).toEqual({ error: "boom: not found" });
    });
  }

  it("does not implement the ingest_file tool (follow-up)", () => {
    const client = stubClient();
    const tools = buildKnowledgeTools(client, "repo-a");
    const names = tools.map((t) => t.name);
    expect(names).toContain("agent_knowledge_ingest");
    expect(names).not.toContain("agent_knowledge_ingest_file");
  });

  it("returns exactly the 8 expected tools", () => {
    const client = stubClient();
    const tools = buildKnowledgeTools(client, "repo-a");
    expect(tools.map((t) => t.name).sort()).toEqual(
      [
        "agent_knowledge_get_current_bank",
        "agent_knowledge_list_pages",
        "agent_knowledge_get_page",
        "agent_knowledge_create_page",
        "agent_knowledge_update_page",
        "agent_knowledge_delete_page",
        "agent_knowledge_recall",
        "agent_knowledge_ingest",
      ].sort()
    );
  });
});
