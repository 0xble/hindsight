import { existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { resolveConfig } from "./config";
import { buildHookOutput } from "./hook";

let root: string;
let cacheFile: string;

beforeEach(() => {
  root = mkdtempSync(join(tmpdir(), "hs-hook-"));
  cacheFile = join(root, "cache", "session.json");
});

afterEach(() => {
  rmSync(root, { recursive: true, force: true });
});

describe("buildHookOutput", () => {
  it("first turn injects BOTH reflect and memories", async () => {
    const cfg = resolveConfig({});
    const result = await buildHookOutput({
      harness: "claude-code",
      prompt: "hello",
      cfg,
      client: {
        reflect: async () => "REFLECT_ANSWER",
        recall: async () => [{ text: "MEM_ONE" }],
      },
      cacheFile,
    });
    expect(result).toContain("REFLECT_ANSWER");
    expect(result).toContain("PRECISELY");
    expect(result).toContain("<hindsight_memories>");
    expect(result).toContain("MEM_ONE");
    expect(existsSync(cacheFile)).toBe(true);
    expect(JSON.parse(readFileSync(cacheFile, "utf8"))).toEqual({ answer: "REFLECT_ANSWER" });
  });

  it("later turn: recall only, reflect NOT called", async () => {
    mkdirSync(join(root, "cache"), { recursive: true });
    writeFileSync(cacheFile, JSON.stringify({ answer: "OLD_REFLECT" }));
    const reflectSpy = vi.fn(async () => {
      throw new Error("reflect must not be called on a later turn");
    });
    const cfg = resolveConfig({});
    const result = await buildHookOutput({
      harness: "claude-code",
      prompt: "hello again",
      cfg,
      client: {
        reflect: reflectSpy,
        recall: async () => [{ text: "MEM_TWO" }],
      },
      cacheFile,
    });
    expect(result).toContain("<hindsight_memories>");
    expect(result).toContain("MEM_TWO");
    expect(result).not.toContain("OLD_REFLECT");
    expect(reflectSpy).not.toHaveBeenCalled();
  });

  it("both empty -> undefined", async () => {
    const cfg = resolveConfig({});
    const result = await buildHookOutput({
      harness: "claude-code",
      prompt: "hello",
      cfg,
      client: {
        reflect: async () => "",
        recall: async () => [],
      },
      cacheFile,
    });
    expect(result).toBeUndefined();
    expect(JSON.parse(readFileSync(cacheFile, "utf8"))).toEqual({ answer: "" });
  });

  it("recall still injects if reflect rejects", async () => {
    const cfg = resolveConfig({});
    const result = await buildHookOutput({
      harness: "claude-code",
      prompt: "hello",
      cfg,
      client: {
        reflect: async () => {
          throw new Error("boom");
        },
        recall: async () => [{ text: "MEM_R" }],
      },
      cacheFile,
    });
    expect(result).toContain("<hindsight_memories>");
    expect(result).toContain("MEM_R");
    expect(JSON.parse(readFileSync(cacheFile, "utf8"))).toEqual({ answer: "" });
  });

  it("reflect still injects if recall rejects", async () => {
    const cfg = resolveConfig({});
    const result = await buildHookOutput({
      harness: "claude-code",
      prompt: "hello",
      cfg,
      client: {
        reflect: async () => "R_ANS",
        recall: async () => {
          throw new Error("recall boom");
        },
      },
      cacheFile,
    });
    expect(result).toContain("R_ANS");
    expect(result).toContain("PRECISELY");
    expect(result).not.toContain("<hindsight_memories>");
  });

  it("both reflect and recall reject -> undefined, no throw", async () => {
    const cfg = resolveConfig({});
    const result = await buildHookOutput({
      harness: "claude-code",
      prompt: "hello",
      cfg,
      client: {
        reflect: async () => {
          throw new Error("reflect boom");
        },
        recall: async () => {
          throw new Error("recall boom");
        },
      },
      cacheFile,
    });
    expect(result).toBeUndefined();
  });

  it("threads recallMaxTokens/recallTimeoutMs config into recall", async () => {
    const cfg = resolveConfig({ recallMaxTokens: 512, recallTimeoutMs: 7000 });
    const recallSpy = vi.fn(async () => [{ text: "MEM" }]);
    await buildHookOutput({
      harness: "claude-code",
      prompt: "the prompt",
      cfg,
      client: {
        reflect: async () => "R",
        recall: recallSpy,
      },
      cacheFile,
    });
    expect(recallSpy).toHaveBeenCalledWith("the prompt", { maxTokens: 512, timeoutMs: 7000 });
  });
});
