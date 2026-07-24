import { mkdtempSync, rmSync, writeFileSync, mkdirSync } from "node:fs";
import { EventEmitter } from "node:events";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  readSeedState,
  writeSeedState,
  startBackgroundSeed,
  seedControl,
  DEFAULT_SEED_LIMIT,
} from "./seed";

let stateDir: string;

beforeEach(() => {
  stateDir = mkdtempSync(join(tmpdir(), "hs-seed-"));
});

afterEach(() => {
  rmSync(stateDir, { recursive: true, force: true });
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

  it("readSeedState on valid-but-non-object JSON (e.g. a bare string) returns {}", () => {
    mkdirSync(stateDir, { recursive: true });
    writeFileSync(
      join(stateDir, encodeURIComponent("bank-1") + ".json"),
      JSON.stringify("just a string")
    );
    expect(readSeedState("bank-1", stateDir)).toEqual({});
  });

  it("writeSeedState never throws when the parent path is blocked by a file", () => {
    const blocker = join(stateDir, "blocker");
    writeFileSync(blocker, "x");
    const brokenDir = join(blocker, "sub");
    expect(() => writeSeedState("b", { declined: true }, brokenDir)).not.toThrow();
    expect(() => readSeedState("b", brokenDir)).not.toThrow();
    expect(readSeedState("b", brokenDir)).toEqual({});
  });
});

describe("startBackgroundSeed", () => {
  function fakeSpawn() {
    return vi.fn().mockReturnValue({ on: vi.fn(), unref: vi.fn() });
  }

  it("spawns node against backfillPath, detached + stdio ignore, with the default limit", () => {
    const spawn = fakeSpawn();
    startBackgroundSeed("/some/repo", { backfillPath: "/dist/backfill.js", spawn });
    expect(spawn).toHaveBeenCalledWith(
      "node",
      ["/dist/backfill.js", "--repo", "/some/repo", "--limit", String(DEFAULT_SEED_LIMIT)],
      { detached: true, stdio: "ignore" }
    );
    expect(spawn.mock.results[0].value.unref).toHaveBeenCalled();
  });

  it("opts.limit overrides the default limit", () => {
    const spawn = fakeSpawn();
    startBackgroundSeed("/some/repo", { backfillPath: "/dist/backfill.js", spawn, limit: 50 });
    expect(spawn).toHaveBeenCalledWith(
      "node",
      ["/dist/backfill.js", "--repo", "/some/repo", "--limit", "50"],
      { detached: true, stdio: "ignore" }
    );
  });

  it("fail-safe: a spawn that throws does not throw out of startBackgroundSeed", () => {
    const spawn = vi.fn().mockImplementation(() => {
      throw new Error("spawn EMFILE");
    });
    expect(() =>
      startBackgroundSeed("/some/repo", { backfillPath: "/dist/backfill.js", spawn })
    ).not.toThrow();
  });

  it("fail-safe: an async 'error' event on the child (ENOENT/EACCES/sandbox) does not crash the caller", () => {
    // Real spawn() failures like ENOENT arrive as an async 'error' event on the returned
    // ChildProcess, not as a synchronous throw — so the spy must be a real EventEmitter to
    // exercise that path (a plain object with unref() wouldn't emit anything).
    const child = new EventEmitter() as EventEmitter & { unref: () => void };
    child.unref = vi.fn();
    const spawn = vi.fn().mockReturnValue(child);
    expect(() =>
      startBackgroundSeed("/some/repo", { backfillPath: "/dist/backfill.js", spawn })
    ).not.toThrow();
    // Emitting 'error' with no listener would normally throw (EventEmitter semantics) and crash
    // the process — proving a listener was attached means this does NOT throw.
    expect(() => child.emit("error", new Error("ENOENT"))).not.toThrow();
  });
});

describe("seedControl", () => {
  function fakeSpawn() {
    return vi.fn().mockReturnValue({ on: vi.fn(), unref: vi.fn() });
  }

  it("seed: spawns the background backfill and persists seededAt", () => {
    const spawn = fakeSpawn();
    const result = seedControl("seed", { repo: "/r", bankId: "b", spawn, stateDir });
    expect(spawn).toHaveBeenCalled();
    const state = readSeedState("b", stateDir);
    expect(typeof state.seededAt).toBe("string");
    expect(state.seededAt).not.toBe("");
    expect(result.ok).toBe(true);
  });

  it("decline: persists declined without spawning", () => {
    const spawn = fakeSpawn();
    const result = seedControl("decline", { repo: "/r", bankId: "b", spawn, stateDir });
    expect(spawn).not.toHaveBeenCalled();
    expect(readSeedState("b", stateDir)).toEqual({ declined: true });
    expect(result.ok).toBe(true);
  });

  it("decline: works fine without a spawn injected at all", () => {
    const result = seedControl("decline", { repo: "/r", bankId: "b", stateDir });
    expect(readSeedState("b", stateDir)).toEqual({ declined: true });
    expect(result.ok).toBe(true);
  });

  it("unknown command: not ok, no state written, no spawn", () => {
    const spawn = fakeSpawn();
    const result = seedControl("bogus", { repo: "/r", bankId: "b", spawn, stateDir });
    expect(spawn).not.toHaveBeenCalled();
    expect(readSeedState("b", stateDir)).toEqual({});
    expect(result.ok).toBe(false);
  });
});
