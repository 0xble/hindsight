import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { buildSessionStartContext, runSessionStartHook } from "./session-start";
import { readSeedState, writeSeedState } from "./seed";
import { resolveConfig } from "./config";

let stateDir: string;

beforeEach(() => {
  stateDir = mkdtempSync(join(tmpdir(), "hs-session-start-"));
});

afterEach(() => {
  rmSync(stateDir, { recursive: true, force: true });
});

/** Default roster the mock client returns; asserted on by name below. */
const listPagesOk = async () => ({ items: [{ id: "p1", name: "Component map" }] });

describe("buildSessionStartContext", () => {
  it("cold git repo + autoSeed on -> starts the seed + survey, writes seededAt, returns note + roster preamble", async () => {
    const client = { listDocumentIds: async () => new Set<string>(), listPages: listPagesOk };
    const startSeed = vi.fn();
    const startSurvey = vi.fn();
    const ctx = await buildSessionStartContext({
      cwd: "/repo/dir",
      bankId: "bank-1",
      cfg: resolveConfig(),
      client,
      stateDir,
      hasGit: () => true,
      startSeed,
      startSurvey,
    });
    expect(startSeed).toHaveBeenCalledWith("/repo/dir", { limit: 300 });
    expect(startSurvey).toHaveBeenCalledWith("/repo/dir", { model: "haiku", budgetUsd: 0.5 });
    const state = readSeedState("bank-1", stateDir);
    expect(typeof state.seededAt).toBe("string");
    expect(state.seededAt).not.toBe("");
    expect(ctx).toBeDefined();
    expect(ctx).toContain("bank-1");
    expect(ctx).toContain("🧠");
    // New: the knowledge preamble lists live pages by name, not the old static mission text.
    expect(ctx).toContain("<hindsight_knowledge>");
    expect(ctx).toContain("- Component map (p1)");
    expect(ctx).not.toContain("agent_knowledge_list_pages");
  });

  it("cold git repo + codebaseSurvey:false -> starts the seed but NOT the survey", async () => {
    const client = { listDocumentIds: async () => new Set<string>(), listPages: listPagesOk };
    const startSeed = vi.fn();
    const startSurvey = vi.fn();
    const ctx = await buildSessionStartContext({
      cwd: "/repo/dir",
      bankId: "bank-1",
      cfg: resolveConfig({ codebaseSurvey: false }),
      client,
      stateDir,
      hasGit: () => true,
      startSeed,
      startSurvey,
    });
    expect(startSeed).toHaveBeenCalledWith("/repo/dir", { limit: 300 });
    expect(startSurvey).not.toHaveBeenCalled();
    expect(ctx).toBeDefined();
  });

  it("non-git dir -> no seed, listDocumentIds not called, roster preamble only (no learning note)", async () => {
    const startSeed = vi.fn();
    let called = false;
    const client = {
      listDocumentIds: async () => {
        called = true;
        return new Set<string>();
      },
      listPages: listPagesOk,
    };
    const ctx = await buildSessionStartContext({
      cwd: "/repo/dir",
      bankId: "bank-1",
      cfg: resolveConfig(),
      client,
      stateDir,
      hasGit: () => false,
      startSeed,
    });
    expect(startSeed).not.toHaveBeenCalled();
    expect(called).toBe(false);
    expect(ctx).toContain("<hindsight_knowledge>");
    expect(ctx).toContain("- Component map (p1)");
    expect(ctx).not.toContain("🧠");
  });

  it("already-seeded state -> no seed, roster preamble only", async () => {
    writeSeedState("bank-1", { seededAt: "2026-01-01T00:00:00Z" }, stateDir);
    const startSeed = vi.fn();
    let called = false;
    const client = {
      listDocumentIds: async () => {
        called = true;
        return new Set<string>();
      },
      listPages: listPagesOk,
    };
    const ctx = await buildSessionStartContext({
      cwd: "/repo/dir",
      bankId: "bank-1",
      cfg: resolveConfig(),
      client,
      stateDir,
      hasGit: () => true,
      startSeed,
    });
    expect(startSeed).not.toHaveBeenCalled();
    expect(called).toBe(false);
    expect(ctx).toContain("<hindsight_knowledge>");
    expect(ctx).toContain("- Component map (p1)");
    expect(ctx).not.toContain("🧠");
  });

  it("declined state -> no seed, roster preamble only", async () => {
    writeSeedState("bank-1", { declined: true }, stateDir);
    const startSeed = vi.fn();
    let called = false;
    const client = {
      listDocumentIds: async () => {
        called = true;
        return new Set<string>();
      },
      listPages: listPagesOk,
    };
    const ctx = await buildSessionStartContext({
      cwd: "/repo/dir",
      bankId: "bank-1",
      cfg: resolveConfig(),
      client,
      stateDir,
      hasGit: () => true,
      startSeed,
    });
    expect(startSeed).not.toHaveBeenCalled();
    expect(called).toBe(false);
    expect(ctx).toContain("<hindsight_knowledge>");
    expect(ctx).toContain("- Component map (p1)");
    expect(ctx).not.toContain("🧠");
  });

  it("warm bank (non-empty doc set) -> no seed, writes seededAt, roster preamble only", async () => {
    const startSeed = vi.fn();
    const client = { listDocumentIds: async () => new Set(["git:abc"]), listPages: listPagesOk };
    const ctx = await buildSessionStartContext({
      cwd: "/repo/dir",
      bankId: "bank-1",
      cfg: resolveConfig(),
      client,
      stateDir,
      hasGit: () => true,
      startSeed,
    });
    expect(startSeed).not.toHaveBeenCalled();
    const state = readSeedState("bank-1", stateDir);
    expect(typeof state.seededAt).toBe("string");
    expect(state.seededAt).not.toBe("");
    expect(ctx).toContain("<hindsight_knowledge>");
    expect(ctx).toContain("- Component map (p1)");
    expect(ctx).not.toContain("🧠");
  });

  it("listDocumentIds throws (server unreachable) -> no seed, NO state written, roster preamble only", async () => {
    const startSeed = vi.fn();
    const client = {
      listDocumentIds: async () => {
        throw new Error("network down");
      },
      listPages: listPagesOk,
    };
    const ctx = await buildSessionStartContext({
      cwd: "/repo/dir",
      bankId: "bank-1",
      cfg: resolveConfig(),
      client,
      stateDir,
      hasGit: () => true,
      startSeed,
    });
    expect(startSeed).not.toHaveBeenCalled();
    expect(readSeedState("bank-1", stateDir)).toEqual({});
    expect(ctx).toContain("<hindsight_knowledge>");
    expect(ctx).toContain("- Component map (p1)");
    expect(ctx).not.toContain("🧠");
  });

  it("listPages rejects -> fail-open: empty-state preamble, seed still starts (cold repo)", async () => {
    const startSeed = vi.fn();
    const client = {
      listDocumentIds: async () => new Set<string>(),
      listPages: async () => {
        throw new Error("pages endpoint down");
      },
    };
    const ctx = await buildSessionStartContext({
      cwd: "/repo/dir",
      bankId: "bank-1",
      cfg: resolveConfig(),
      client,
      stateDir,
      hasGit: () => true,
      startSeed,
    });
    // Seeding is unaffected by a listPages failure.
    expect(startSeed).toHaveBeenCalledWith("/repo/dir", { limit: 300 });
    expect(ctx).toBeDefined();
    // Empty-state roster preamble still renders (no page names, no throw).
    expect(ctx).toContain("<hindsight_knowledge>");
    expect(ctx).toContain("No knowledge pages yet");
    // No roster entry (the page-id marker only appears when a page is listed).
    expect(ctx).not.toContain("(p1)");
    // The background-learning note is still present.
    expect(ctx).toContain("🧠");
  });

  it("autoSeed:false -> skips the whole seed branch (no listDocumentIds call), roster preamble only", async () => {
    const startSeed = vi.fn();
    let called = false;
    const client = {
      listDocumentIds: async () => {
        called = true;
        return new Set<string>();
      },
      listPages: listPagesOk,
    };
    const ctx = await buildSessionStartContext({
      cwd: "/repo/dir",
      bankId: "bank-1",
      cfg: resolveConfig({ autoSeed: false }),
      client,
      stateDir,
      hasGit: () => true,
      startSeed,
    });
    expect(startSeed).not.toHaveBeenCalled();
    expect(called).toBe(false);
    expect(ctx).toContain("<hindsight_knowledge>");
    expect(ctx).toContain("- Component map (p1)");
    expect(ctx).not.toContain("🧠");
  });
});

describe("runSessionStartHook anti-recursion guard", () => {
  const ORIGINAL = process.env.HINDSIGHT_DISABLE_HOOKS;

  afterEach(() => {
    if (ORIGINAL === undefined) delete process.env.HINDSIGHT_DISABLE_HOOKS;
    else process.env.HINDSIGHT_DISABLE_HOOKS = ORIGINAL;
  });

  it("HINDSIGHT_DISABLE_HOOKS set -> returns immediately, never reads stdin or builds a client", async () => {
    process.env.HINDSIGHT_DISABLE_HOOKS = "1";
    const makeClient = vi.fn();
    // No stdin is provided/mocked here — if the guard didn't return before `readFileSync(0, ...)`,
    // this call would attempt to read the real process stdin. Resolving without calling makeClient
    // proves the guard fired first.
    await runSessionStartHook(makeClient);
    expect(makeClient).not.toHaveBeenCalled();
  });
});
