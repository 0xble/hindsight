import { describe, expect, it } from "vitest";
import { buildSystemInjection } from "./inject";

describe("buildSystemInjection", () => {
  const MEMORY = "The retain hook batches every 10 user turns.";
  const out = buildSystemInjection(MEMORY);

  it("includes the surfaced memory verbatim", () => {
    expect(out).toContain(MEMORY);
  });

  it("keeps the precise-application guidance", () => {
    expect(out).toContain("PRECISELY");
    expect(out).toContain("Verify against the current code");
  });

  it("emits the visible attribution header directive", () => {
    expect(out).toContain("🧠 **Using Hindsight Memories**");
  });

  it("renders real emoji + em dash, never literal escape sequences", () => {
    expect(out).toContain("🧠");
    expect(out).toContain("—");
    expect(out).not.toMatch(/\\u[0-9a-f]{4}/i);
  });

  it("only surrogate-free code points (no lone surrogates)", () => {
    for (const ch of out) {
      const cp = ch.codePointAt(0)!;
      expect(cp < 0xd800 || cp > 0xdfff).toBe(true);
    }
  });
});
