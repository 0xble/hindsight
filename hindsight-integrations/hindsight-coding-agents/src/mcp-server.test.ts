import { describe, expect, it } from "vitest";
import { selectTools } from "./mcp-server";
import { resolveConfig } from "./core/config";
import type { HindsightClient } from "./core/hindsight";

// Plain stub — no SDK, no network. selectTools only needs a client reference to hand to
// buildKnowledgeTools when enabled; it never calls any client method itself.
const stubClient = {} as HindsightClient;

describe("selectTools", () => {
  it("returns [] when cfg.disabled is true — a disabled Hindsight exposes NO tools", () => {
    const cfg = resolveConfig({ disabled: true });
    expect(selectTools(cfg, stubClient, "b")).toEqual([]);
  });

  it("returns the 8 agent_knowledge_* tool specs when enabled", () => {
    const cfg = resolveConfig({});
    const tools = selectTools(cfg, stubClient, "b");
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
