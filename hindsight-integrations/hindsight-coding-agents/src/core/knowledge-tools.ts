/**
 * Knowledge-page MCP tool specs — SDK-free so this stays unit-testable without a real MCP host.
 *
 * `src/mcp-server.ts` is the only file that imports the MCP SDK; it wires the specs returned here
 * into an `McpServer`. Each tool wraps one `HindsightClient` knowledge-page/recall method: it never
 * throws — a thrown client error is caught and turned into an `isError:true` text result so the
 * calling LLM sees the failure instead of the process crashing.
 *
 * NOTE: `agent_knowledge_ingest` / `agent_knowledge_ingest_file` (raw-content retain) are NOT
 * implemented here yet — follow-up task once the ingest flow is designed for MCP.
 */
import { z } from "zod";
import type { ZodRawShape } from "zod";
import type { HindsightClient } from "./hindsight";

export interface ToolResult {
  // Index signature so this structurally satisfies the MCP SDK's CallToolResult (which carries
  // extra optional fields we don't set) when passed to `McpServer.tool()` in src/mcp-server.ts —
  // the only file that imports the SDK; this file stays SDK-free.
  [x: string]: unknown;
  content: { type: "text"; text: string }[];
  isError?: boolean;
}

export interface ToolSpec {
  name: string;
  description: string;
  inputSchema: ZodRawShape;
  handler: (args: any) => Promise<ToolResult>;
}

function ok(value: unknown): ToolResult {
  return { content: [{ type: "text", text: JSON.stringify(value, null, 2) }] };
}

function err(e: unknown): ToolResult {
  const message = String((e as Error)?.message ?? e);
  return { content: [{ type: "text", text: JSON.stringify({ error: message }) }], isError: true };
}

/** Wrap a handler body so a thrown client error always becomes an isError:true result, never a throw. */
function guarded(fn: (args: any) => Promise<unknown>): (args: any) => Promise<ToolResult> {
  return async (args: any) => {
    try {
      return ok(await fn(args));
    } catch (e) {
      return err(e);
    }
  };
}

/** Build the knowledge-page + recall MCP tool specs, bound to one client/bank. */
export function buildKnowledgeTools(client: HindsightClient, bankId: string): ToolSpec[] {
  return [
    {
      name: "agent_knowledge_get_current_bank",
      description:
        "Get the memory bank id this MCP server (and the recall/retain hooks) resolved for the " +
        "current repo/worktree. All knowledge-page and recall operations here share this one bank.",
      inputSchema: {},
      handler: async (_args: Record<string, never>) => ok({ bank_id: bankId }),
    },
    {
      name: "agent_knowledge_list_pages",
      description: "List knowledge pages (ids + names). Use get_page for full content.",
      inputSchema: {},
      handler: guarded(async () => client.listPages()),
    },
    {
      name: "agent_knowledge_get_page",
      description: "Get one knowledge page's synthesized content by id.",
      inputSchema: { page_id: z.string() },
      handler: guarded(async ({ page_id }) => client.getPage(page_id)),
    },
    {
      name: "agent_knowledge_create_page",
      description:
        "Create a new knowledge page. source_query is a question that gets re-asked against the " +
        "bank after every consolidation to rebuild the page's content — pages auto-update over " +
        "time as new conversations/facts accrue, no manual refresh needed.",
      inputSchema: {
        page_id: z.string(),
        name: z.string(),
        source_query: z.string(),
      },
      handler: guarded(async ({ page_id, name, source_query }) =>
        client.createPage(page_id, name, source_query)
      ),
    },
    {
      name: "agent_knowledge_update_page",
      description: "Update a knowledge page's name and/or source_query.",
      inputSchema: {
        page_id: z.string(),
        name: z.string().optional(),
        source_query: z.string().optional(),
      },
      handler: guarded(async ({ page_id, name, source_query }) =>
        client.updatePage(page_id, { name, sourceQuery: source_query })
      ),
    },
    {
      name: "agent_knowledge_delete_page",
      description: "Permanently delete a knowledge page.",
      inputSchema: { page_id: z.string() },
      handler: guarded(async ({ page_id }) => client.deletePage(page_id)),
    },
    {
      name: "agent_knowledge_recall",
      description: "Recall (search) memories in the current bank for a query.",
      inputSchema: {
        query: z.string(),
        max_tokens: z.number().optional(),
      },
      handler: guarded(async ({ query, max_tokens }) =>
        client.recall(query, { maxTokens: max_tokens })
      ),
    },
  ];
}
