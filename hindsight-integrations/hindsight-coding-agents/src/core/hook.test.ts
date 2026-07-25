import { existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { resolveConfig } from "./config";
import { buildHookOutput, runHook } from "./hook";

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
  it("injects the memories block (recall-only, no reflect)", async () => {
    const cfg = resolveConfig({});
    const result = await buildHookOutput({
      harness: "claude-code",
      prompt: "hello",
      cfg,
      client: {
        recall: async () => [{ text: "MEM_ONE" }],
        listPages: async () => ({ items: [] }),
      },
      cacheFile,
    });
    expect(result).toContain("<hindsight_memories>");
    expect(result).toContain("MEM_ONE");
    expect(result).toContain("SHOW HINDSIGHT WORKING"); // the attribution directive leads the block
    expect(existsSync(cacheFile)).toBe(true);
    expect(JSON.parse(readFileSync(cacheFile, "utf8"))).toEqual({ turns: 1 });
  });

  it("empty recall -> undefined", async () => {
    const cfg = resolveConfig({});
    const result = await buildHookOutput({
      harness: "claude-code",
      prompt: "hello",
      cfg,
      client: { recall: async () => [], listPages: async () => ({ items: [] }) },
      cacheFile,
    });
    expect(result).toBeUndefined();
    expect(JSON.parse(readFileSync(cacheFile, "utf8"))).toEqual({ turns: 1 });
  });

  it("recall rejects -> undefined, no throw, turn still counted", async () => {
    const cfg = resolveConfig({});
    const result = await buildHookOutput({
      harness: "claude-code",
      prompt: "hello",
      cfg,
      client: {
        recall: async () => {
          throw new Error("recall boom");
        },
        listPages: async () => ({ items: [] }),
      },
      cacheFile,
    });
    expect(result).toBeUndefined();
    expect(JSON.parse(readFileSync(cacheFile, "utf8"))).toEqual({ turns: 1 });
  });

  it("threads recallMaxTokens/recallTimeoutMs config into recall", async () => {
    const cfg = resolveConfig({ recallMaxTokens: 512, recallTimeoutMs: 7000 });
    const recallSpy = vi.fn(async () => [{ text: "MEM" }]);
    await buildHookOutput({
      harness: "claude-code",
      prompt: "the prompt",
      cfg,
      client: { recall: recallSpy, listPages: async () => ({ items: [] }) },
      cacheFile,
    });
    expect(recallSpy).toHaveBeenCalledWith("the prompt", { maxTokens: 512, timeoutMs: 7000 });
  });

  it("defaults the recall token budget to 750", async () => {
    const cfg = resolveConfig({});
    const recallSpy = vi.fn(async () => [{ text: "MEM" }]);
    await buildHookOutput({
      harness: "claude-code",
      prompt: "the prompt",
      cfg,
      client: { recall: recallSpy, listPages: async () => ({ items: [] }) },
      cacheFile,
    });
    expect(recallSpy).toHaveBeenCalledWith("the prompt", { maxTokens: 750, timeoutMs: 10000 });
  });

  it("persists and increments the turn counter each call", async () => {
    const cfg = resolveConfig({ pageRefreshEveryTurns: 3 });
    for (let n = 1; n <= 4; n++) {
      await buildHookOutput({
        harness: "claude-code",
        prompt: `turn ${n}`,
        cfg,
        client: {
          recall: async () => [],
          listPages: async () => ({ items: [{ id: "p1", name: "Component map" }] }),
        },
        cacheFile,
      });
      const cached = JSON.parse(readFileSync(cacheFile, "utf8")) as { turns?: number };
      expect(cached.turns).toBe(n);
    }
  });

  it("injects the page-roster refresh only on cadence turns", async () => {
    const cfg = resolveConfig({ pageRefreshEveryTurns: 2 });
    const client = {
      recall: async () => [{ text: "MEM" }],
      listPages: async () => ({ items: [{ id: "p1", name: "Component map" }] }),
    };
    // turn 1: not a multiple of 2 -> no refresh
    const t1 = await buildHookOutput({
      harness: "claude-code",
      prompt: "one",
      cfg,
      client,
      cacheFile,
    });
    expect(t1).not.toContain("Component map");
    expect(t1).not.toContain("hindsight_knowledge_refresh");
    // turn 2: multiple of 2 -> refresh injected
    const t2 = await buildHookOutput({
      harness: "claude-code",
      prompt: "two",
      cfg,
      client,
      cacheFile,
    });
    expect(t2).toContain("hindsight_knowledge_refresh");
    expect(t2).toContain("Component map");
    // memories still present alongside the refresh block
    expect(t2).toContain("<hindsight_memories>");
  });

  it("listPages rejection on a cadence turn still injects the tool+capture reminder (no roster), recall intact, no throw", async () => {
    const cfg = resolveConfig({ pageRefreshEveryTurns: 2 });
    // Seed the cache so this is turn 2 (a cadence turn) without needing a first turn.
    mkdirSync(join(root, "cache"), { recursive: true });
    writeFileSync(cacheFile, JSON.stringify({ turns: 1 }));
    const result = await buildHookOutput({
      harness: "claude-code",
      prompt: "two",
      cfg,
      client: {
        recall: async () => [{ text: "MEM_KEEP" }],
        listPages: async () => {
          throw new Error("listPages boom");
        },
      },
      cacheFile,
    });
    expect(result).toContain("<hindsight_memories>");
    expect(result).toContain("MEM_KEEP");
    // The reminder re-injects even when listPages fails (empty roster fallback) — the nudge
    // must not depend on a page existing.
    expect(result).toContain("<hindsight_knowledge_refresh>");
    expect(result).toContain("hindsight_capture_initiative");
    expect(result).not.toContain("Current Hindsight knowledge pages");
    // turns still advanced despite the listPages failure
    expect(JSON.parse(readFileSync(cacheFile, "utf8")).turns).toBe(2);
  });
});

describe("runHook anti-recursion guard", () => {
  const ORIGINAL = process.env.HINDSIGHT_DISABLE_HOOKS;

  afterEach(() => {
    if (ORIGINAL === undefined) delete process.env.HINDSIGHT_DISABLE_HOOKS;
    else process.env.HINDSIGHT_DISABLE_HOOKS = ORIGINAL;
  });

  it("HINDSIGHT_DISABLE_HOOKS set -> returns immediately, never reads stdin or builds a client", async () => {
    process.env.HINDSIGHT_DISABLE_HOOKS = "1";
    const makeClient = vi.fn();
    // No stdin is provided/mocked here — if the guard didn't return before `readFileSync(0, ...)`,
    // this call would attempt to read the real process stdin. The fact this resolves at all (let
    // alone without calling makeClient) proves the guard fired first.
    await runHook({ harness: "claude-code", parse: () => ({}), emit: (c) => ({ c }) }, makeClient);
    expect(makeClient).not.toHaveBeenCalled();
  });
});
