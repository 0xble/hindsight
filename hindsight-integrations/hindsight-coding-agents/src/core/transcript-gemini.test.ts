import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { readGeminiTranscript } from "./transcript-gemini";

let root: string;
let file: string;

beforeEach(() => {
  root = mkdtempSync(join(tmpdir(), "hs-gemini-transcript-"));
  file = join(root, "session.jsonl");
});
afterEach(() => {
  rmSync(root, { recursive: true, force: true });
});

describe("readGeminiTranscript", () => {
  it("reconstructs the mutation log: user text + assistant string; drops synthetic/reasoning/tool results", () => {
    const lines = [
      // header line: dropped
      JSON.stringify({ kind: "main", sessionId: "s1", projectHash: "h", startTime: "t" }),
      // initial $set.messages seeds the synthetic session-context user message (dropped)
      JSON.stringify({
        $set: {
          messages: [
            {
              id: "ctx",
              type: "user",
              content: [{ text: "<session_context>\nproject tree…\n</session_context>" }],
            },
          ],
        },
      }),
      // real user prompt: appended, kept
      JSON.stringify({ id: "u1", type: "user", content: [{ text: "how many files are here?" }] }),
      // metadata bump: ignored
      JSON.stringify({ $set: { lastUpdated: "t2" } }),
      // assistant streaming placeholder (empty string) — later superseded by same id
      JSON.stringify({
        id: "a1",
        type: "gemini",
        content: "",
        thoughts: [{ subject: "thinking" }],
      }),
      // assistant final (same id a1 → last-wins content)
      JSON.stringify({
        id: "a1",
        type: "gemini",
        content: "Let me check.",
        thoughts: [{ subject: "x" }],
      }),
      // tool result: a user message whose content is a functionResponse → dropped entirely
      JSON.stringify({
        id: "t1",
        type: "user",
        content: [
          { functionResponse: { name: "list_directory", response: { output: "47 items" } } },
        ],
      }),
      // final assistant answer
      JSON.stringify({ id: "a2", type: "gemini", content: "There are 47 files." }),
    ];
    writeFileSync(file, lines.join("\n"));

    expect(readGeminiTranscript(file)).toEqual([
      { role: "user", content: "how many files are here?" },
      { role: "assistant", content: "Let me check." },
      { role: "assistant", content: "There are 47 files." },
    ]);
  });

  it("renders a functionCall part as a compact role:'action' turn (name + primary target, no args)", () => {
    const lines = [
      JSON.stringify({ id: "u1", type: "user", content: [{ text: "list files" }] }),
      JSON.stringify({
        id: "a1",
        type: "gemini",
        content: [{ functionCall: { name: "run_shell_command", args: { command: "ls" } } }],
      }),
    ];
    writeFileSync(file, lines.join("\n"));
    expect(readGeminiTranscript(file)).toEqual([
      { role: "user", content: "list files" },
      { role: "action", content: "run_shell_command ls" },
    ]);
  });

  it("strips injected memory that leaks into a kept message", () => {
    writeFileSync(
      file,
      JSON.stringify({
        id: "u1",
        type: "user",
        content: [{ text: "<hindsight_memories>\nleak\n</hindsight_memories>\nWhy retry?" }],
      })
    );
    expect(readGeminiTranscript(file)).toEqual([{ role: "user", content: "Why retry?" }]);
  });

  it("drops functionResponse tool outputs entirely — even a huge one produces no turn", () => {
    writeFileSync(
      file,
      JSON.stringify({
        id: "t1",
        type: "user",
        content: [
          { functionResponse: { name: "read_file", response: { output: "x".repeat(5000) } } },
        ],
      })
    );
    expect(readGeminiTranscript(file)).toEqual([]);
  });

  it("fails open (returns []) when the file cannot be read", () => {
    expect(readGeminiTranscript(join(root, "nope.jsonl"))).toEqual([]);
  });
});
