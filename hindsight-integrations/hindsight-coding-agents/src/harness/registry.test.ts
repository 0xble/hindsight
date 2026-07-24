import { describe, expect, it } from "vitest";
import { getHarness, HARNESS_NAMES } from "./registry";

describe("HARNESS_NAMES", () => {
  it("lists all registered harnesses", () => {
    expect(HARNESS_NAMES).toEqual(
      expect.arrayContaining(["opencode", "claude-code", "cursor-cli", "codex"])
    );
    expect(HARNESS_NAMES).toHaveLength(4);
  });
});

describe("getHarness", () => {
  it("resolves hook harnesses without touching the opencode adapter", async () => {
    for (const name of ["claude-code", "cursor-cli", "codex"]) {
      const adapter = await getHarness(name);
      expect(adapter.name).toBe(name);
      // Lightweight hook adapters have no persistent runtime — createRuntime always throws before
      // touching its argument, so a stand-in value is fine here.
      expect(() => adapter.createRuntime({} as never)).toThrow();
    }
  });

  it("resolves the opencode adapter by name", async () => {
    const adapter = await getHarness("opencode");
    expect(adapter.name).toBe("opencode");
  });

  it("rejects unknown harness names", async () => {
    await expect(getHarness("nope")).rejects.toThrow(/unknown harness/);
  });
});
