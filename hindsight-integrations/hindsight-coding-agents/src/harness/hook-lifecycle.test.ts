import { describe, expect, it } from "vitest";
import { HOOK_HARNESSES, type HookHarnessName } from "./hook-lifecycle";

const HOOK_HARNESS_NAMES: HookHarnessName[] = ["claude-code", "codex", "gemini", "cursor-cli"];

describe("HOOK_HARNESSES lifecycle contract", () => {
  it("declares every lifecycle once for every hook-based harness", () => {
    for (const harness of HOOK_HARNESS_NAMES) {
      expect(Object.keys(HOOK_HARNESSES[harness].install).sort()).toEqual([
        "prompt",
        "sessionStart",
        "stop",
      ]);
      expect(HOOK_HARNESSES[harness].sessionStart.harness).toBe(harness);
      expect(HOOK_HARNESSES[harness].prompt.harness).toBe(harness);
      expect(HOOK_HARNESSES[harness].retain.harness).toBe(harness);
    }
  });

  it("keeps the runtime schema and installed event names in the same host declaration", () => {
    const cursor = HOOK_HARNESSES["cursor-cli"];
    expect(cursor.install).toMatchObject({
      sessionStart: { event: "sessionStart", entry: "cursor-sessionstart-hook.js" },
      prompt: { event: "beforeSubmitPrompt", entry: "cursor-hook.js" },
      stop: { event: "stop", entry: "cursor-stop-hook.js" },
    });
    expect(
      cursor.sessionStart.emit({ systemMessage: "visible", additionalContext: "context" })
    ).toEqual({
      additional_context: "context",
    });
    expect(cursor.prompt.emit("context", "visible")).toEqual({
      continue: true,
      additional_context: "context",
    });

    const claude = HOOK_HARNESSES["claude-code"];
    expect(claude.install.prompt.event).toBe("UserPromptSubmit");
    expect(claude.sessionStart.emit({ additionalContext: "context" })).toEqual({
      hookSpecificOutput: {
        hookEventName: "SessionStart",
        additionalContext: "context",
      },
    });
  });
});
