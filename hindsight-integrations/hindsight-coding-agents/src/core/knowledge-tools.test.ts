import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { basename, join } from "node:path";
import { describe, expect, it, vi } from "vitest";
import { buildKnowledgeTools } from "./knowledge-tools";
import type { HindsightClient } from "./hindsight";

/** Minimal stub of the HindsightClient surface the tools call — no SDK, no network. */
function stubClient(overrides: Partial<Record<string, ReturnType<typeof vi.fn>>> = {}) {
  return {
    listPages: vi.fn(async () => ({ pages: [] })),
    getPage: vi.fn(async (_id: string) => ({ id: _id })),
    recall: vi.fn(async (_q: string, _o: unknown) => [{ text: "a fact" }]),
    captureInitiative: vi.fn(async (_a: unknown) => ({ page_id: "initiative-x" })),
    retain: vi.fn(async (..._args: unknown[]) => undefined),
    ...overrides,
  } as unknown as HindsightClient;
}

function findTool(tools: ReturnType<typeof buildKnowledgeTools>, name: string) {
  const t = tools.find((t) => t.name === name);
  if (!t) throw new Error(`tool not found: ${name}`);
  return t;
}

const EXPECTED_TOOLS = [
  "hindsight_sync_status",
  "hindsight_get_current_bank",
  "hindsight_search_knowledge_pages",
  "hindsight_list_knowledge_pages",
  "hindsight_read_knowledge_page",
  "hindsight_search_memory",
  "hindsight_capture_initiative",
  "hindsight_ingest_document",
];

describe("buildKnowledgeTools", () => {
  it("returns exactly the eight expected tools (as a set)", () => {
    const client = stubClient();
    const tools = buildKnowledgeTools(client, "repo-a");
    expect(tools.map((t) => t.name).sort()).toEqual([...EXPECTED_TOOLS].sort());
  });

  it("hindsight_sync_status is the FIRST tool in the list", () => {
    const client = stubClient();
    const tools = buildKnowledgeTools(client, "repo-a");
    expect(tools[0].name).toBe("hindsight_sync_status");
  });

  it("does not expose the removed raw page-CRUD tools", () => {
    const client = stubClient();
    const names = buildKnowledgeTools(client, "repo-a").map((t) => t.name);
    expect(names).not.toContain("create_page");
    expect(names).not.toContain("update_page");
    expect(names).not.toContain("delete_page");
    expect(names).not.toContain("agent_knowledge_create_page");
    expect(names).not.toContain("agent_knowledge_update_page");
    expect(names).not.toContain("agent_knowledge_delete_page");
  });

  it("hindsight_sync_status returns the syncStatus JSON for the given repoDir", async () => {
    const repoDir = mkdtempSync(join(tmpdir(), "hs-status-nogit-"));
    try {
      const repoName = basename(repoDir);
      const client = stubClient({
        listDocumentIds: vi.fn(async (tag: string) =>
          tag === "source:git"
            ? new Set([`gitlog:${repoName}`, "git:sha1", "git:sha2"])
            : new Set(["chat:s1"])
        ),
        listPages: vi.fn(async () => ({ items: [{ id: "p1", name: "Component map" }] })),
        activeOperations: vi.fn(async () => 0),
      });
      const tools = buildKnowledgeTools(client, "repo-a", { repoDir });
      const tool = findTool(tools, "hindsight_sync_status");
      const result = await tool.handler({});
      expect(result.isError).toBeFalsy();
      expect(JSON.parse(result.content[0].text)).toEqual({
        bank: "repo-a",
        gitlogPresent: true,
        gitDiffDocs: 2,
        gitDiffTarget: null, // temp dir is not a git repo
        chatDocs: 1,
        pagesCount: 1,
        activeOps: 0,
        synced: true,
      });
    } finally {
      rmSync(repoDir, { recursive: true, force: true });
    }
  });

  it("hindsight_sync_status returns isError:true when the client's listDocumentIds throws", async () => {
    const repoDir = mkdtempSync(join(tmpdir(), "hs-status-nogit-"));
    try {
      const client = stubClient({
        listDocumentIds: vi.fn(async () => {
          throw new Error("server down");
        }),
        listPages: vi.fn(async () => ({ items: [] })),
        activeOperations: vi.fn(async () => 0),
      });
      const tools = buildKnowledgeTools(client, "repo-a", { repoDir });
      const tool = findTool(tools, "hindsight_sync_status");
      const result = await tool.handler({});
      expect(result.isError).toBe(true);
      expect(JSON.parse(result.content[0].text)).toEqual({ error: "server down" });
    } finally {
      rmSync(repoDir, { recursive: true, force: true });
    }
  });

  it("hindsight_get_current_bank returns {bank_id} without touching the client", async () => {
    const client = stubClient();
    const tools = buildKnowledgeTools(client, "repo-a");
    const tool = findTool(tools, "hindsight_get_current_bank");
    const result = await tool.handler({});
    expect(result.isError).toBeFalsy();
    expect(JSON.parse(result.content[0].text)).toEqual({ bank_id: "repo-a" });
    expect(client.listPages).not.toHaveBeenCalled();
    expect(client.getPage).not.toHaveBeenCalled();
    expect(client.recall).not.toHaveBeenCalled();
  });

  it("hindsight_search_knowledge_pages returns section objects with page/page_id/heading/content", async () => {
    const client = stubClient({
      listPages: vi.fn(async () => ({
        items: [
          { id: "p1", name: "Uploader guide" },
          { id: "p2", name: "Auth notes" },
        ],
      })),
      getPage: vi.fn(async (id: string) =>
        id === "p1"
          ? { content: "Preamble prose about uploads.\n\n## Retry backoff\nUploads retry.\n" }
          : { content: "Tokens rotate daily.\n" }
      ),
    });
    const tools = buildKnowledgeTools(client, "repo-a");
    const tool = findTool(tools, "hindsight_search_knowledge_pages");
    const result = await tool.handler({ query: "anything" });
    expect(result.isError).toBeFalsy();
    expect(client.listPages).toHaveBeenCalledTimes(1);
    expect(client.getPage).toHaveBeenCalledWith("p1");
    expect(client.getPage).toHaveBeenCalledWith("p2");
    // Interim stub: the FIRST section of each distinct page, regardless of the query.
    expect(JSON.parse(result.content[0].text)).toEqual([
      {
        page: "Uploader guide",
        page_id: "p1",
        heading: "Uploader guide",
        content: "Preamble prose about uploads.",
      },
      {
        page: "Auth notes",
        page_id: "p2",
        heading: "Auth notes",
        content: "Tokens rotate daily.",
      },
    ]);
  });

  it("hindsight_search_knowledge_pages returns isError:true when listPages throws", async () => {
    const client = stubClient({
      listPages: vi.fn(async () => {
        throw new Error("pages down");
      }),
    });
    const tools = buildKnowledgeTools(client, "repo-a");
    const tool = findTool(tools, "hindsight_search_knowledge_pages");
    const result = await tool.handler({ query: "anything" });
    expect(result.isError).toBe(true);
    expect(JSON.parse(result.content[0].text)).toEqual({ error: "pages down" });
  });

  it("hindsight_list_knowledge_pages calls client.listPages() with no args", async () => {
    const client = stubClient({ listPages: vi.fn(async () => ({ pages: [{ id: "p1" }] })) });
    const tools = buildKnowledgeTools(client, "repo-a");
    const tool = findTool(tools, "hindsight_list_knowledge_pages");
    const result = await tool.handler({});
    expect(client.listPages).toHaveBeenCalledWith();
    expect(JSON.parse(result.content[0].text)).toEqual({ pages: [{ id: "p1" }] });
  });

  it("hindsight_read_knowledge_page calls client.getPage(page_id)", async () => {
    const client = stubClient({ getPage: vi.fn(async (id: string) => ({ id, name: "X" })) });
    const tools = buildKnowledgeTools(client, "repo-a");
    const tool = findTool(tools, "hindsight_read_knowledge_page");
    const result = await tool.handler({ page_id: "p1" });
    expect(client.getPage).toHaveBeenCalledWith("p1");
    expect(JSON.parse(result.content[0].text)).toEqual({ id: "p1", name: "X" });
  });

  it("hindsight_search_memory calls client.recall(query, {maxTokens: max_tokens})", async () => {
    const client = stubClient();
    const tools = buildKnowledgeTools(client, "repo-a");
    const tool = findTool(tools, "hindsight_search_memory");
    const result = await tool.handler({ query: "how do we validate?", max_tokens: 512 });
    expect(client.recall).toHaveBeenCalledWith("how do we validate?", { maxTokens: 512 });
    expect(JSON.parse(result.content[0].text)).toEqual([{ text: "a fact" }]);
  });

  it("hindsight_search_memory passes maxTokens: undefined when max_tokens omitted", async () => {
    const client = stubClient();
    const tools = buildKnowledgeTools(client, "repo-a");
    const tool = findTool(tools, "hindsight_search_memory");
    await tool.handler({ query: "q" });
    expect(client.recall).toHaveBeenCalledWith("q", { maxTokens: undefined });
  });

  it("hindsight_capture_initiative calls client.captureInitiative({title, summary, relatesToPageId}) and returns the page id", async () => {
    const client = stubClient({
      captureInitiative: vi.fn(async (_a: unknown) => ({ page_id: "initiative-retry-backoff" })),
    });
    const tools = buildKnowledgeTools(client, "repo-a");
    const tool = findTool(tools, "hindsight_capture_initiative");
    const result = await tool.handler({
      title: "Retry backoff for the uploader",
      summary: "Add exponential backoff so transient upload failures self-heal.",
      relates_to_page_id: "initiative-uploader",
    });
    expect(client.captureInitiative).toHaveBeenCalledWith({
      title: "Retry backoff for the uploader",
      summary: "Add exponential backoff so transient upload failures self-heal.",
      relatesToPageId: "initiative-uploader",
    });
    expect(JSON.parse(result.content[0].text)).toEqual({ page_id: "initiative-retry-backoff" });
  });

  it("hindsight_capture_initiative passes relatesToPageId: undefined for a brand-new initiative", async () => {
    const client = stubClient();
    const tools = buildKnowledgeTools(client, "repo-a");
    const tool = findTool(tools, "hindsight_capture_initiative");
    await tool.handler({ title: "New thing", summary: "why" });
    expect(client.captureInitiative).toHaveBeenCalledWith({
      title: "New thing",
      summary: "why",
      relatesToPageId: undefined,
    });
  });

  it("hindsight_ingest_document slugifies the title and calls client.retain(...) with the 'document' strategy", async () => {
    const client = stubClient();
    const tools = buildKnowledgeTools(client, "repo-a");
    const tool = findTool(tools, "hindsight_ingest_document");
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

  it("hindsight_ingest_document collapses internal whitespace runs in the title into single hyphens", async () => {
    const client = stubClient();
    const tools = buildKnowledgeTools(client, "repo-a");
    const tool = findTool(tools, "hindsight_ingest_document");
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

  it("hindsight_ingest_document strips punctuation from the title into a safe slug", async () => {
    const client = stubClient();
    const tools = buildKnowledgeTools(client, "repo-a");
    const tool = findTool(tools, "hindsight_ingest_document");
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

  it("hindsight_ingest_document falls back to 'doc' when the title has no safe characters", async () => {
    const client = stubClient();
    const tools = buildKnowledgeTools(client, "repo-a");
    const tool = findTool(tools, "hindsight_ingest_document");
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
    "hindsight_list_knowledge_pages",
    "hindsight_read_knowledge_page",
    "hindsight_search_memory",
    "hindsight_capture_initiative",
    "hindsight_ingest_document",
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
        recall: vi.fn(async () => {
          throw boom;
        }),
        captureInitiative: vi.fn(async () => {
          throw boom;
        }),
        retain: vi.fn(async () => {
          throw boom;
        }),
      });
      const tools = buildKnowledgeTools(client, "repo-a");
      const tool = findTool(tools, name);
      const args =
        name === "hindsight_search_memory"
          ? { query: "q" }
          : name === "hindsight_capture_initiative"
            ? { title: "T", summary: "S" }
            : name === "hindsight_ingest_document"
              ? { title: "T", content: "C" }
              : name === "hindsight_read_knowledge_page"
                ? { page_id: "p1" }
                : {};
      const result = await tool.handler(args);
      expect(result.isError).toBe(true);
      expect(JSON.parse(result.content[0].text)).toEqual({ error: "boom: not found" });
    });
  }
});
