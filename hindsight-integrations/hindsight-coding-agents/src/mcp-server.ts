#!/usr/bin/env node
/**
 * Native TS MCP (stdio) server exposing the `agent_knowledge_*` knowledge-page + recall tools.
 *
 * Bank resolution MUST mirror the hooks exactly (loadConfig + deriveBankId, harness
 * "claude-code") so knowledge pages, recall, and retain all land in ONE per-repo bank — this is
 * why this is a native TS server and not a reuse of the Python MCP (whose bank derivation
 * differs). `HINDSIGHT_MCP_PROJECT_CWD` lets the launching host tell us which project directory
 * to resolve the bank for, since an MCP server's own `process.cwd()` isn't guaranteed to be the
 * user's project directory.
 */
import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { loadConfig } from "./core/config";
import { deriveBankId } from "./core/bank";
import { HindsightClient } from "./core/hindsight";
import { buildKnowledgeTools } from "./core/knowledge-tools";

async function main() {
  const cwd = process.env.HINDSIGHT_MCP_PROJECT_CWD || process.cwd();
  const cfg = loadConfig({ harness: "claude-code", projectDir: cwd });
  const bankId = deriveBankId(cfg, cwd, "claude-code");
  const client = new HindsightClient({ apiUrl: cfg.apiUrl, apiToken: cfg.apiToken, bank: bankId });

  const server = new McpServer({ name: "hindsight", version: "0.1.0" });
  for (const t of buildKnowledgeTools(client, bankId)) {
    server.tool(t.name, t.description, t.inputSchema, t.handler);
  }

  await server.connect(new StdioServerTransport());
}

main().catch((e) => {
  console.error("hindsight mcp-server failed:", e);
  process.exit(1);
});
