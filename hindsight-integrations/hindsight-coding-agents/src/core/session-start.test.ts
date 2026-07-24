import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { buildSessionOffer } from "./session-start";
import { readSeedState, writeSeedState } from "./seed";

let stateDir: string;

beforeEach(() => {
  stateDir = mkdtempSync(join(tmpdir(), "hs-session-start-"));
});

afterEach(() => {
  rmSync(stateDir, { recursive: true, force: true });
});

describe("buildSessionOffer", () => {
  it("cold repo + git repo + no prior state -> returns the offer string", async () => {
    const client = { listDocumentIds: async () => new Set<string>() };
    const offer = await buildSessionOffer({
      cwd: "/repo/dir",
      bankId: "bank-1",
      pluginRoot: "/plugin/root",
      client,
      stateDir,
      hasGit: () => true,
    });
    expect(offer).toBeDefined();
    expect(offer).toContain("/repo/dir");
    expect(offer).toContain('/plugin/root/dist/hindsight-seed.js" seed --repo');
    expect(offer).toContain("decline --repo");
    expect(offer).toContain("/plugin/root");
  });

  it("non-git dir -> undefined, client not called", async () => {
    let called = false;
    const client = {
      listDocumentIds: async () => {
        called = true;
        return new Set<string>();
      },
    };
    const offer = await buildSessionOffer({
      cwd: "/repo/dir",
      bankId: "bank-1",
      pluginRoot: "/plugin/root",
      client,
      stateDir,
      hasGit: () => false,
    });
    expect(offer).toBeUndefined();
    expect(called).toBe(false);
  });

  it("declined state -> undefined, client not called", async () => {
    writeSeedState("bank-1", { declined: true }, stateDir);
    let called = false;
    const client = {
      listDocumentIds: async () => {
        called = true;
        return new Set<string>();
      },
    };
    const offer = await buildSessionOffer({
      cwd: "/repo/dir",
      bankId: "bank-1",
      pluginRoot: "/plugin/root",
      client,
      stateDir,
      hasGit: () => true,
    });
    expect(offer).toBeUndefined();
    expect(called).toBe(false);
  });

  it("already-seeded state -> undefined, client not called", async () => {
    writeSeedState("bank-1", { seededAt: "2026-01-01T00:00:00Z" }, stateDir);
    let called = false;
    const client = {
      listDocumentIds: async () => {
        called = true;
        return new Set<string>();
      },
    };
    const offer = await buildSessionOffer({
      cwd: "/repo/dir",
      bankId: "bank-1",
      pluginRoot: "/plugin/root",
      client,
      stateDir,
      hasGit: () => true,
    });
    expect(offer).toBeUndefined();
    expect(called).toBe(false);
  });

  it("warm bank (non-empty doc set) -> undefined AND writes seededAt so we don't re-enumerate", async () => {
    const client = { listDocumentIds: async () => new Set(["git:abc"]) };
    const offer = await buildSessionOffer({
      cwd: "/repo/dir",
      bankId: "bank-1",
      pluginRoot: "/plugin/root",
      client,
      stateDir,
      hasGit: () => true,
    });
    expect(offer).toBeUndefined();
    const state = readSeedState("bank-1", stateDir);
    expect(typeof state.seededAt).toBe("string");
    expect(state.seededAt).not.toBe("");
  });

  it("server unreachable (client throws) -> undefined AND no state is written (transient outage)", async () => {
    const client = {
      listDocumentIds: async () => {
        throw new Error("network down");
      },
    };
    const offer = await buildSessionOffer({
      cwd: "/repo/dir",
      bankId: "bank-1",
      pluginRoot: "/plugin/root",
      client,
      stateDir,
      hasGit: () => true,
    });
    expect(offer).toBeUndefined();
    expect(readSeedState("bank-1", stateDir)).toEqual({});
  });
});
