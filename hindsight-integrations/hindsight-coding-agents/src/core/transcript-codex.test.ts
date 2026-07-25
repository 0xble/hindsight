import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { readCodexTranscript } from "./transcript-codex";

let root: string;
let file: string;

beforeEach(() => {
  root = mkdtempSync(join(tmpdir(), "hs-codex-transcript-"));
  file = join(root, "rollout.jsonl");
});
afterEach(() => {
  rmSync(root, { recursive: true, force: true });
});

const item = (payload: unknown) => JSON.stringify({ type: "response_item", payload });

describe("readCodexTranscript", () => {
  it("keeps user/assistant text + function calls/outputs; drops developer/synthetic/reasoning/injected", () => {
    const lines = [
      // non-response_item line: dropped
      JSON.stringify({ type: "session_meta", payload: { cwd: "/repo" } }),
      // developer message (Codex system prompt + our injected context): dropped entirely
      item({
        type: "message",
        role: "developer",
        content: [{ type: "input_text", text: "<permissions instructions>…</permissions>" }],
      }),
      item({
        type: "message",
        role: "developer",
        content: [
          {
            type: "input_text",
            text: "<hindsight_memories>\nsecret recalled fact\n</hindsight_memories>",
          },
        ],
      }),
      // synthetic startup user message: dropped
      item({
        type: "message",
        role: "user",
        content: [
          { type: "input_text", text: "# AGENTS.md instructions for /repo\n<INSTRUCTIONS>x</INSTRUCTIONS>" },
          { type: "input_text", text: "<environment_context>\n<cwd>/repo</cwd>\n</environment_context>" },
        ],
      }),
      // real user prompt: kept
      item({
        type: "message",
        role: "user",
        content: [{ type: "input_text", text: "add retry backoff to the uploader" }],
      }),
      // reasoning: dropped
      item({ type: "reasoning", id: "rs_1", encrypted_content: "…" }),
      // assistant commentary: kept
      item({
        type: "message",
        role: "assistant",
        phase: "commentary",
        content: [{ type: "output_text", text: "I'll add exponential backoff." }],
      }),
      // tool call: kept
      item({
        type: "function_call",
        name: "exec_command",
        arguments: '{"cmd":"npm test"}',
        call_id: "call_1",
      }),
      // tool result: kept, role "tool"
      item({ type: "function_call_output", call_id: "call_1", output: "12 passed" }),
      // assistant final answer: kept
      item({
        type: "message",
        role: "assistant",
        phase: "final_answer",
        content: [{ type: "output_text", text: "Done — backoff added, tests pass." }],
      }),
      // malformed line + non-object: tolerated
      "{ not json",
      "null",
    ];
    writeFileSync(file, lines.join("\n"));

    const turns = readCodexTranscript(file);

    expect(turns).toEqual([
      { role: "user", content: "add retry backoff to the uploader" },
      { role: "assistant", content: "I'll add exponential backoff." },
      { role: "assistant", content: '**exec_command** {"cmd":"npm test"}' },
      { role: "tool", content: "↳ 12 passed" },
      { role: "assistant", content: "Done — backoff added, tests pass." },
    ]);
  });

  it("strips injected memory that leaks into a kept (user/assistant) message", () => {
    writeFileSync(
      file,
      item({
        type: "message",
        role: "user",
        content: [
          { type: "input_text", text: "<hindsight_memories>\nleak\n</hindsight_memories>\nWhy retry?" },
        ],
      })
    );
    const turns = readCodexTranscript(file);
    expect(turns).toEqual([{ role: "user", content: "Why retry?" }]);
  });

  it("truncates a very long function_call_output to the 2000-char cap", () => {
    writeFileSync(
      file,
      item({ type: "function_call_output", call_id: "c", output: "x".repeat(5000) })
    );
    const turns = readCodexTranscript(file);
    expect(turns).toHaveLength(1);
    expect(turns[0].role).toBe("tool");
    expect(turns[0].content).toContain("… (truncated)");
    expect(turns[0].content.length).toBeLessThan(2100);
  });

  it("fails open (returns []) when the file cannot be read", () => {
    expect(readCodexTranscript(join(root, "nope.jsonl"))).toEqual([]);
  });
});
