import { afterEach, describe, expect, it, vi } from "vitest";
import { buildSessionStartContext, runSessionStartHook } from "./session-start";
import { resolveConfig } from "./config";

/** Default roster the mock client returns; asserted on by name below. */
const listPagesOk = async () => ({ items: [{ id: "p1", name: "Component map" }] });

describe("buildSessionStartContext", () => {
  it("cold git repo + autoSeed on -> seeds + surveys, note in systemMessage (user-visible) + roster in additionalContext (model)", async () => {
    const client = { listDocumentIds: async () => new Set<string>(), listPages: listPagesOk };
    const startSeed = vi.fn();
    const startSurvey = vi.fn();
    const out = await buildSessionStartContext({
      cwd: "/repo/dir",
      bankId: "bank-1",
      cfg: resolveConfig(),
      client,
      hasGit: () => true,
      startSeed,
      startSurvey,
    });
    expect(startSeed).toHaveBeenCalledWith("/repo/dir", { limit: 300 });
    expect(startSurvey).toHaveBeenCalledWith("/repo/dir", {
      harness: "claude-code",
      model: "haiku",
      budgetUsd: 2,
    });
    // The learning note is USER-VISIBLE (systemMessage), not buried in model context.
    expect(out.systemMessage).toContain("is learning");
    expect(out.systemMessage).toContain("bank-1");
    // The knowledge preamble is model context, lists live pages, and drops the old static mission.
    expect(out.additionalContext).toContain("<hindsight_knowledge>");
    expect(out.additionalContext).toContain("- Component map (p1)");
    expect(out.additionalContext).not.toContain("agent_knowledge_list_pages");
    // The banner must NOT be duplicated into model context. (The tool guide legitimately
    // contains a "🧠 From Hindsight memory" attribution example, so match on banner text.)
    expect(out.additionalContext).not.toContain("memory bank");
    expect(out.additionalContext).not.toContain("is learning this repo");
  });

  it("cold git repo + codebaseSurvey:false -> starts the seed but NOT the survey", async () => {
    const client = { listDocumentIds: async () => new Set<string>(), listPages: listPagesOk };
    const startSeed = vi.fn();
    const startSurvey = vi.fn();
    const out = await buildSessionStartContext({
      cwd: "/repo/dir",
      bankId: "bank-1",
      cfg: resolveConfig({ codebaseSurvey: false }),
      client,
      hasGit: () => true,
      startSeed,
      startSurvey,
    });
    expect(startSeed).toHaveBeenCalledWith("/repo/dir", { limit: 300 });
    expect(startSurvey).not.toHaveBeenCalled();
    expect(out.systemMessage).toContain("is learning");
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
    const out = await buildSessionStartContext({
      cwd: "/repo/dir",
      bankId: "bank-1",
      cfg: resolveConfig(),
      client,
      hasGit: () => false,
      startSeed,
    });
    expect(startSeed).not.toHaveBeenCalled();
    expect(called).toBe(false);
    expect(out.additionalContext).toContain("- Component map (p1)");
    // banner shows on EVERY session now; non-cold paths use the "remembering" wording
    expect(out.systemMessage).toContain("remembering");
  });

  // The old "declined state -> no seed" test is gone with the seed-state file itself: the live
  // bank is the ONLY state now, so there is no client-side declined flag to consult. Opting a
  // repo out of memory is `disabled` in project config.

  it("cold-check-wins: EMPTY live bank -> (re)seeds — the bank is the only state", async () => {
    const startSeed = vi.fn();
    let called = false;
    const client = {
      listDocumentIds: async () => {
        called = true;
        return new Set<string>(); // bank is empty (fresh, or user cleared it)
      },
      listPages: listPagesOk,
    };
    const out = await buildSessionStartContext({
      cwd: "/repo/dir",
      bankId: "bank-1",
      cfg: resolveConfig(),
      client,
      hasGit: () => true,
      startSeed,
    });
    // The live bank is consulted, and an empty bank seeds — no client-side flag can contradict it.
    expect(called).toBe(true);
    expect(startSeed).toHaveBeenCalledWith("/repo/dir", { limit: 300 });
    expect(out.systemMessage).toContain("is learning");
  });

  it("warm bank (non-empty doc set) -> deepen engine fires, but no survey/note", async () => {
    const startSeed = vi.fn();
    const startSurvey = vi.fn();
    const client = { listDocumentIds: async () => new Set(["git:abc"]), listPages: listPagesOk };
    const out = await buildSessionStartContext({
      cwd: "/repo/dir",
      bankId: "bank-1",
      cfg: resolveConfig(),
      client,
      hasGit: () => true,
      startSeed,
      startSurvey,
    });
    // The engine is idempotent, so every warm session start re-fires it to pick up missing work.
    expect(startSeed).toHaveBeenCalledWith("/repo/dir", { limit: 300 });
    // The cold-only extras stay off: no survey, no user-facing learning note.
    expect(startSurvey).not.toHaveBeenCalled();
    expect(out.additionalContext).toContain("- Component map (p1)");
    // banner shows on EVERY session now; non-cold paths use the "remembering" wording
    expect(out.systemMessage).toContain("remembering");
  });

  it("listDocumentIds throws (server unreachable) -> no seed, roster preamble only", async () => {
    const startSeed = vi.fn();
    const client = {
      listDocumentIds: async () => {
        throw new Error("network down");
      },
      listPages: listPagesOk,
    };
    const out = await buildSessionStartContext({
      cwd: "/repo/dir",
      bankId: "bank-1",
      cfg: resolveConfig(),
      client,
      hasGit: () => true,
      startSeed,
    });
    expect(startSeed).not.toHaveBeenCalled();
    expect(out.additionalContext).toContain("- Component map (p1)");
    // banner shows on EVERY session now; non-cold paths use the "remembering" wording
    expect(out.systemMessage).toContain("remembering");
  });

  it("listPages rejects -> fail-open: empty-state preamble, seed still starts, note still visible (cold repo)", async () => {
    const startSeed = vi.fn();
    const client = {
      listDocumentIds: async () => new Set<string>(),
      listPages: async () => {
        throw new Error("pages endpoint down");
      },
    };
    const out = await buildSessionStartContext({
      cwd: "/repo/dir",
      bankId: "bank-1",
      cfg: resolveConfig(),
      client,
      hasGit: () => true,
      startSeed,
    });
    // Seeding is unaffected by a listPages failure.
    expect(startSeed).toHaveBeenCalledWith("/repo/dir", { limit: 300 });
    // Empty-state roster preamble still renders (no page names, no throw).
    expect(out.additionalContext).toContain("<hindsight_knowledge>");
    expect(out.additionalContext).toContain("No knowledge pages yet");
    expect(out.additionalContext).not.toContain("(p1)");
    // The background-learning note is still user-visible.
    expect(out.systemMessage).toContain("is learning");
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
    const out = await buildSessionStartContext({
      cwd: "/repo/dir",
      bankId: "bank-1",
      cfg: resolveConfig({ autoSeed: false }),
      client,
      hasGit: () => true,
      startSeed,
    });
    expect(startSeed).not.toHaveBeenCalled();
    expect(called).toBe(false);
    expect(out.additionalContext).toContain("- Component map (p1)");
    // banner shows on EVERY session now; non-cold paths use the "remembering" wording
    expect(out.systemMessage).toContain("remembering");
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
    await runSessionStartHook("claude-code", makeClient);
    expect(makeClient).not.toHaveBeenCalled();
  });
});
