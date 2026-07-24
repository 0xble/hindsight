import { describe, expect, it } from "vitest";
import { formatMemories } from "./recall";

describe("formatMemories", () => {
  it("wraps results in <hindsight_memories> with the 🧠 attribution preamble and lists text", () => {
    const out = formatMemories([{ text: "we use zod for validation" }, { text: "retry policy is 3x" }]);
    expect(out).toContain("<hindsight_memories>");
    expect(out).toContain("🧠 **Using Hindsight Memories**");
    expect(out).toContain("we use zod for validation");
    expect(out).toContain("retry policy is 3x");
    expect(out).toContain("</hindsight_memories>");
  });

  it("returns empty string for no results", () => {
    expect(formatMemories([])).toBe("");
  });
});
