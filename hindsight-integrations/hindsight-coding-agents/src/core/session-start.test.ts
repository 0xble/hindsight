import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { buildSessionStartContext, KNOWLEDGE_MISSION } from "./session-start";
import { readSeedState, writeSeedState } from "./seed";
import { resolveConfig } from "./config";

let stateDir: string;

beforeEach(() => {
  stateDir = mkdtempSync(join(tmpdir(), "hs-session-start-"));
});

afterEach(() => {
  rmSync(stateDir, { recursive: true, force: true });
});

describe("buildSessionStartContext", () => {
  it("cold git repo + autoSeed on -> starts the seed, writes seededAt, returns note + mission", async () => {
    const client = { listDocumentIds: async () => new Set<string>() };
    const startSeed = vi.fn();
    const ctx = await buildSessionStartContext({
      cwd: "/repo/dir",
      bankId: "bank-1",
      cfg: resolveConfig(),
      client,
      stateDir,
      hasGit: () => true,
      startSeed,
    });
    expect(startSeed).toHaveBeenCalledWith("/repo/dir", { limit: 300 });
    const state = readSeedState("bank-1", stateDir);
    expect(typeof state.seededAt).toBe("string");
    expect(state.seededAt).not.toBe("");
    expect(ctx).toBeDefined();
    expect(ctx).toContain("bank-1");
    expect(ctx).toContain("🧠");
    expect(ctx).toContain(KNOWLEDGE_MISSION);
    expect(ctx).toContain("<hindsight_knowledge>");
  });

  it("non-git dir -> no seed, client not called, mission only (no learning note)", async () => {
    const startSeed = vi.fn();
    let called = false;
    const client = {
      listDocumentIds: async () => {
        called = true;
        return new Set<string>();
      },
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
    expect(ctx).toContain(KNOWLEDGE_MISSION);
    expect(ctx).not.toContain("🧠");
  });

  it("already-seeded state -> no seed, mission only", async () => {
    writeSeedState("bank-1", { seededAt: "2026-01-01T00:00:00Z" }, stateDir);
    const startSeed = vi.fn();
    let called = false;
    const client = {
      listDocumentIds: async () => {
        called = true;
        return new Set<string>();
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
    expect(startSeed).not.toHaveBeenCalled();
    expect(called).toBe(false);
    expect(ctx).toBe(KNOWLEDGE_MISSION);
  });

  it("declined state -> no seed, mission only", async () => {
    writeSeedState("bank-1", { declined: true }, stateDir);
    const startSeed = vi.fn();
    let called = false;
    const client = {
      listDocumentIds: async () => {
        called = true;
        return new Set<string>();
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
    expect(startSeed).not.toHaveBeenCalled();
    expect(called).toBe(false);
    expect(ctx).toBe(KNOWLEDGE_MISSION);
  });

  it("warm bank (non-empty doc set) -> no seed, writes seededAt, mission only", async () => {
    const startSeed = vi.fn();
    const client = { listDocumentIds: async () => new Set(["git:abc"]) };
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
    expect(ctx).toBe(KNOWLEDGE_MISSION);
  });

  it("listDocumentIds throws (server unreachable) -> no seed, NO state written, mission only", async () => {
    const startSeed = vi.fn();
    const client = {
      listDocumentIds: async () => {
        throw new Error("network down");
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
    expect(startSeed).not.toHaveBeenCalled();
    expect(readSeedState("bank-1", stateDir)).toEqual({});
    expect(ctx).toBe(KNOWLEDGE_MISSION);
  });

  it("autoSeed:false -> skips the whole seed branch (no client call), mission only", async () => {
    const startSeed = vi.fn();
    let called = false;
    const client = {
      listDocumentIds: async () => {
        called = true;
        return new Set<string>();
      },
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
    expect(ctx).toBe(KNOWLEDGE_MISSION);
  });
});
