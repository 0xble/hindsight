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
  it("first turn injects BOTH reflect and memories", async () => {
    const cfg = resolveConfig({});
    const result = await buildHookOutput({
      harness: "claude-code",
      prompt: "hello",
      cfg,
      client: {
        reflect: async () => "REFLECT_ANSWER",
        recall: async () => [{ text: "MEM_ONE" }],
        listPages: async () => ({ items: [] }),
      },
      cacheFile,
    });
    expect(result).toContain("REFLECT_ANSWER");
    expect(result).toContain("PRECISELY");
    expect(result).toContain("<hindsight_memories>");
    expect(result).toContain("MEM_ONE");
    expect(existsSync(cacheFile)).toBe(true);
    expect(JSON.parse(readFileSync(cacheFile, "utf8"))).toEqual({
      answer: "REFLECT_ANSWER",
      turns: 1,
    });
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
        listPages: async () => ({ items: [] }),
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
        listPages: async () => ({ items: [] }),
      },
      cacheFile,
    });
    expect(result).toBeUndefined();
    expect(JSON.parse(readFileSync(cacheFile, "utf8"))).toEqual({ answer: "", turns: 1 });
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
        listPages: async () => ({ items: [] }),
      },
      cacheFile,
    });
    expect(result).toContain("<hindsight_memories>");
    expect(result).toContain("MEM_R");
    expect(JSON.parse(readFileSync(cacheFile, "utf8"))).toEqual({ answer: "", turns: 1 });
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
        listPages: async () => ({ items: [] }),
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
        listPages: async () => ({ items: [] }),
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
        listPages: async () => ({ items: [] }),
      },
      cacheFile,
    });
    expect(recallSpy).toHaveBeenCalledWith("the prompt", { maxTokens: 512, timeoutMs: 7000 });
  });

  it("caps reflect's timeoutMs to the hook kill window even when cfg.reflectTimeoutMs is huge", async () => {
    // Claude Code's UserPromptSubmit hook is killed at 15s; cfg.reflectTimeoutMs defaults to
    // 120000 for the opencode RuntimeCore path. The hook must cap it so reflect always
    // resolves/aborts before the external kill (see HOOK_REFLECT_CAP_MS in hook.ts).
    const cfg = resolveConfig({ reflectTimeoutMs: 120000 });
    const reflectSpy = vi.fn(async () => "R");
    await buildHookOutput({
      harness: "claude-code",
      prompt: "the prompt",
      cfg,
      client: {
        reflect: reflectSpy,
        recall: async () => [],
        listPages: async () => ({ items: [] }),
      },
      cacheFile,
    });
    expect(reflectSpy).toHaveBeenCalledWith("the prompt", { budget: "high", timeoutMs: 8000 });
  });

  it("uses cfg.reflectTimeoutMs as-is when it's already below the hook cap", async () => {
    const cfg = resolveConfig({ reflectTimeoutMs: 3000 });
    const reflectSpy = vi.fn(async () => "R");
    await buildHookOutput({
      harness: "claude-code",
      prompt: "the prompt",
      cfg,
      client: {
        reflect: reflectSpy,
        recall: async () => [],
        listPages: async () => ({ items: [] }),
      },
      cacheFile,
    });
    expect(reflectSpy).toHaveBeenCalledWith("the prompt", { budget: "high", timeoutMs: 3000 });
  });

  it("persists and increments turns each call, round-tripping {answer, turns}", async () => {
    const cfg = resolveConfig({ pageRefreshEveryTurns: 3 });
    for (let n = 1; n <= 4; n++) {
      await buildHookOutput({
        harness: "claude-code",
        prompt: `turn ${n}`,
        cfg,
        client: {
          reflect: async () => "R",
          recall: async () => [],
          listPages: async () => ({ items: [{ id: "p1", name: "Component map" }] }),
        },
        cacheFile,
      });
      const cached = JSON.parse(readFileSync(cacheFile, "utf8")) as {
        answer?: string;
        turns?: number;
      };
      expect(cached.turns).toBe(n);
      // Reflect runs only on the first turn; its answer persists across later turns.
      expect(cached.answer).toBe("R");
    }
  });

  it("injects the page-roster refresh only on cadence turns", async () => {
    const cfg = resolveConfig({ pageRefreshEveryTurns: 2 });
    const client = {
      reflect: async () => "R",
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

  it("listPages rejection is fail-open on a cadence turn (recall/injection intact, no throw)", async () => {
    const cfg = resolveConfig({ pageRefreshEveryTurns: 2 });
    // Seed the cache so this is turn 2 (a cadence turn) without needing a first turn.
    mkdirSync(join(root, "cache"), { recursive: true });
    writeFileSync(cacheFile, JSON.stringify({ answer: "OLD", turns: 1 }));
    const result = await buildHookOutput({
      harness: "claude-code",
      prompt: "two",
      cfg,
      client: {
        reflect: async () => {
          throw new Error("reflect must not run on a later turn");
        },
        recall: async () => [{ text: "MEM_KEEP" }],
        listPages: async () => {
          throw new Error("listPages boom");
        },
      },
      cacheFile,
    });
    expect(result).toContain("<hindsight_memories>");
    expect(result).toContain("MEM_KEEP");
    expect(result).not.toContain("hindsight_knowledge_refresh");
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
