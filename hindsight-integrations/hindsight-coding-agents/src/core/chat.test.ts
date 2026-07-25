import { describe, expect, it, vi } from "vitest";
import type { HindsightClient } from "./hindsight";
import { renderSessionMarkdown, retainLiveSession, type TransportTurn } from "./chat";

describe("renderSessionMarkdown", () => {
  const turns: TransportTurn[] = [
    { role: "user", content: "Add retry backoff", timestamp: "2026-01-01T00:00:00Z" },
    { role: "assistant", content: "**Edit** uploader.ts", timestamp: "2026-01-01T00:00:01Z" },
    { role: "tool", content: "↳ 12 passed" },
  ];

  it("renders a REF-ID-led markdown transcript with per-role headings", () => {
    const md = renderSessionMarkdown("conversation:s1", turns, "2026-01-01T00:00:00Z");
    expect(md).toContain("REF-ID: conversation:s1");
    expect(md).toContain("## User");
    expect(md).toContain("## Assistant");
    expect(md).toContain("## Tool result"); // role "tool" -> "Tool result" heading
    expect(md).toContain("Add retry backoff");
    expect(md).toContain("**Edit** uploader.ts");
    expect(md).toContain("↳ 12 passed");
    // Not the old JSON array form.
    expect(md).not.toContain('[{"role"');
  });

  it("falls back to the raw role name for an unknown role", () => {
    const md = renderSessionMarkdown(
      "r",
      [{ role: "critic", content: "x" }],
      "2026-01-01T00:00:00Z"
    );
    expect(md).toContain("## critic");
  });
});

describe("retainLiveSession", () => {
  it("upserts a markdown transcript under conversation:<id> with the verbose session strategy", async () => {
    const retain = vi.fn().mockResolvedValue(undefined);
    const client = { retain } as unknown as HindsightClient;

    await retainLiveSession(
      client,
      "s2",
      [{ role: "user", content: "hi", timestamp: "2026-01-01T00:00:00Z" }],
      "2026-01-01T00:00:00Z"
    );

    expect(retain).toHaveBeenCalledTimes(1);
    const [content, context, documentId, tags, strategy, opts] = retain.mock.calls[0];
    expect(content).toContain("REF-ID: conversation:s2");
    expect(content).toContain("## User");
    expect(context).toBe("coding agent session");
    expect(documentId).toBe("conversation:s2");
    expect(tags).toEqual(["source:chat"]);
    expect(strategy).toBe("session");
    expect(opts).toMatchObject({ async: true, timestamp: "2026-01-01T00:00:00Z" });
    expect(opts.metadata).toMatchObject({ source: "chat", session_id: "s2" });
  });
});
