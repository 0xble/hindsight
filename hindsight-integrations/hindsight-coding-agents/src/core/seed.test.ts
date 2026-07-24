import { mkdtempSync, rmSync, writeFileSync, mkdirSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { isColdRepo, readSeedState, writeSeedState } from "./seed";

let stateDir: string;

beforeEach(() => {
  stateDir = mkdtempSync(join(tmpdir(), "hs-seed-"));
});

afterEach(() => {
  rmSync(stateDir, { recursive: true, force: true });
});

describe("isColdRepo", () => {
  it("true when there are no git-sourced docs", async () => {
    const client = { listDocumentIds: async () => new Set<string>() };
    expect(await isColdRepo(client)).toBe(true);
  });

  it("false when git-sourced docs exist", async () => {
    const client = { listDocumentIds: async () => new Set(["git:abc"]) };
    expect(await isColdRepo(client)).toBe(false);
  });

  it("fail-safe: false when the client throws", async () => {
    const client = {
      listDocumentIds: async () => {
        throw new Error("network down");
      },
    };
    expect(await isColdRepo(client)).toBe(false);
  });
});

describe("seed state persistence", () => {
  it("readSeedState on a fresh dir returns {}", () => {
    expect(readSeedState("bank-1", stateDir)).toEqual({});
  });

  it("writeSeedState then readSeedState round-trips", () => {
    writeSeedState("bank-1", { declined: true }, stateDir);
    expect(readSeedState("bank-1", stateDir)).toEqual({ declined: true });
  });

  it("writeSeedState merges patches over prior state", () => {
    writeSeedState("bank-1", { declined: true }, stateDir);
    writeSeedState("bank-1", { seededAt: "2026-01-01T00:00:00Z" }, stateDir);
    expect(readSeedState("bank-1", stateDir)).toEqual({
      declined: true,
      seededAt: "2026-01-01T00:00:00Z",
    });
  });

  it("bank ids with / and : round-trip via filesystem-safe encoding", () => {
    const bankId = "org/repo:main";
    writeSeedState(bankId, { declined: true }, stateDir);
    expect(readSeedState(bankId, stateDir)).toEqual({ declined: true });
  });

  it("readSeedState on a corrupt file returns {} without throwing", () => {
    mkdirSync(stateDir, { recursive: true });
    writeFileSync(join(stateDir, encodeURIComponent("bank-1") + ".json"), "{ not json");
    expect(readSeedState("bank-1", stateDir)).toEqual({});
  });
});
