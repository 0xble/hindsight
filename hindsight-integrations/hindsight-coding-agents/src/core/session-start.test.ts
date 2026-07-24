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
    expect(offer).toContain("node '/plugin/root/dist/hindsight-seed.js' seed --repo '/repo/dir'");
    expect(offer).toContain(
      "node '/plugin/root/dist/hindsight-seed.js' decline --repo '/repo/dir'"
    );
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

  it("shell-escapes a malicious cwd so it can't break out of the quoted --repo argument", async () => {
    const maliciousCwd = '/tmp/x" ; touch /tmp/PWNED ; echo "';
    const client = { listDocumentIds: async () => new Set<string>() };
    const offer = await buildSessionOffer({
      cwd: maliciousCwd,
      bankId: "bank-1",
      pluginRoot: "/plugin/root",
      client,
      stateDir,
      hasGit: () => true,
    });
    expect(offer).toBeDefined();
    // Only the two actual shell command lines matter for injection safety — the human-readable
    // question line above them intentionally echoes the raw cwd as plain text, never executed.
    const commandLines = offer!.split("\n").filter((l) => l.trim().startsWith("node "));
    expect(commandLines).toHaveLength(2);
    for (const line of commandLines) {
      // The cwd must be wrapped in POSIX-safe single quotes, as the LAST token on the line — i.e.
      // nothing after it breaks out to run `touch /tmp/PWNED` unquoted.
      expect(line.trim().endsWith(`--repo '/tmp/x" ; touch /tmp/PWNED ; echo "'`)).toBe(true);
      // No unquoted `;` after the closing quote of --repo's argument.
      const afterRepoArg = line.split(`--repo '/tmp/x" ; touch /tmp/PWNED ; echo "'`)[1] ?? "";
      expect(afterRepoArg.trim()).toBe("");
    }
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
