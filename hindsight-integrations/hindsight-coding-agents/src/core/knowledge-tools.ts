/**
 * Knowledge-page MCP tool specs — SDK-free so this stays unit-testable without a real MCP host.
 *
 * `src/mcp-server.ts` is the only file that imports the MCP SDK; it wires the specs returned here
 * into an `McpServer`. Each tool wraps one `HindsightClient` knowledge-page/recall method: it never
 * throws — a thrown client error is caught and turned into an `isError:true` text result so the
 * calling LLM sees the failure instead of the process crashing.
 *
 * The agent-facing surface is intentionally curated: grounding + capture only. Raw page CRUD
 * (create/update/delete) is deliberately NOT exposed — agents never author page structure; they
 * capture initiatives (`hindsight_capture_initiative`) and pages are synthesized/maintained by the
 * server. `hindsight_ingest_document` (raw-content retain) also backs the codebase survey
 * (core/survey.ts).
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
      name: "hindsight_get_current_bank",
      description:
        "Get the memory bank id this MCP server resolved for the current repo/worktree. All " +
        "knowledge-page and memory operations here share this one bank.",
      inputSchema: {},
      handler: async (_args: Record<string, never>) => ok({ bank_id: bankId }),
    },
    {
      name: "hindsight_list_knowledge_pages",
      description:
        "List this repository's Hindsight knowledge pages — curated, continuously-updated " +
        "summaries of the project's durable knowledge (architecture, components, conventions, key " +
        "decisions, and in-flight initiatives). Returns each page's id, title, and a one-line " +
        "description of what it covers. Call this at the start of any non-trivial task, and again " +
        "periodically in long sessions, to see what the project already knows before you read code " +
        "or ask the user. The list changes as work is captured, so re-check it occasionally.",
      inputSchema: {},
      handler: guarded(async () => client.listPages()),
    },
    {
      name: "hindsight_read_knowledge_page",
      description:
        "Read the full content of one knowledge page by its id (from " +
        "hindsight_list_knowledge_pages). Call this whenever a listed page is relevant to what " +
        "you're about to do — e.g. read Conventions before writing new code, Component map before " +
        "changing a subsystem, or an initiative's page before continuing that feature. A page may " +
        "contain [[page:<id>]] links to related pages; follow one by calling this tool again with " +
        "that id. Prefer reading a page over re-deriving the same understanding from source.",
      inputSchema: { page_id: z.string() },
      handler: guarded(async ({ page_id }) => client.getPage(page_id)),
    },
    {
      name: "hindsight_search_memory",
      description:
        "Search the repository's raw memory (facts from git history, past sessions, and captured " +
        "notes) for specifics a knowledge page doesn't cover — a particular past decision and its " +
        "rationale, why some code is the way it is, prior discussion of a bug, or the detail behind " +
        "an initiative. Use when the pages are too high-level for your question, or to pull the " +
        "underlying facts behind something a page mentions.",
      inputSchema: {
        query: z.string(),
        max_tokens: z.number().optional(),
      },
      handler: guarded(async ({ query, max_tokens }) =>
        client.recall(query, { maxTokens: max_tokens })
      ),
    },
    {
      name: "hindsight_capture_initiative",
      description:
        "Record that a MAJOR new feature, initiative, or substantial enhancement is being worked " +
        "on, so it becomes a first-class, trackable page in this project's knowledge base that " +
        "future sessions can pick up.\n\n" +
        "Call this when: the user asks you to build a significant new capability; you start or make " +
        "real progress on a feature worth remembering across sessions; or a meaningful enhancement " +
        "is made to an existing initiative.\n\n" +
        "Do NOT call this for: routine bug fixes, small tweaks, refactors, chores, or anything a " +
        "teammate wouldn't want summarized weeks later. When in doubt, it is probably not an " +
        "initiative — skip it.\n\n" +
        "- title: short, specific name (e.g. 'Retry backoff for the uploader').\n" +
        "- summary: 2-4 sentences on WHAT is being built/changed and WHY — the intent, not a code " +
        "diff.\n" +
        "- relates_to_page_id: OMIT for a brand-new initiative. To log an enhancement to an " +
        "initiative that already has a page, pass that page's id (from " +
        "hindsight_list_knowledge_pages) so this attaches to it instead of creating a duplicate.\n\n" +
        "Returns the initiative's page id. You never format a page yourself — that's handled for " +
        "you.",
      inputSchema: {
        title: z.string(),
        summary: z.string(),
        relates_to_page_id: z.string().optional(),
      },
      handler: guarded(async ({ title, summary, relates_to_page_id }) =>
        client.captureInitiative({ title, summary, relatesToPageId: relates_to_page_id })
      ),
    },
    {
      name: "hindsight_ingest_document",
      description:
        "Save an external document or a block of durable notes/findings into this repository's " +
        "memory so it informs future recall and pages. Use for design notes, research, or " +
        "reference material you want remembered — not for the conversation you're already in " +
        "(that's captured automatically at session end).",
      inputSchema: { title: z.string(), content: z.string() },
      handler: guarded(async ({ title, content }) => {
        const docId =
          title
            .toLowerCase()
            .replace(/[^a-z0-9]+/g, "-")
            .replace(/^-+|-+$/g, "") || "doc";
        await client.retain(content, "ingested document", docId, ["source:upload"], "document", {
          async: true,
        });
        return { ok: true, doc_id: docId };
      }),
    },
  ];
}
