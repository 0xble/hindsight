import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { readClaudeTranscript } from "./transcript";

let root: string;
let file: string;

beforeEach(() => {
  root = mkdtempSync(join(tmpdir(), "hs-transcript-"));
  file = join(root, "session.jsonl");
});

afterEach(() => {
  rmSync(root, { recursive: true, force: true });
});

describe("readClaudeTranscript", () => {
  it("extracts normalized text turns, dropping non-message/isMeta/text-less lines and tolerating malformed lines", () => {
    const lines = [
      // non-message line: dropped
      JSON.stringify({ type: "last-prompt", leafUuid: "x" }),
      // kept: string content
      JSON.stringify({
        type: "user",
        timestamp: "2026-01-01T00:00:00Z",
        message: { role: "user", content: "how do we validate input?" },
      }),
      // kept: only the text block survives (thinking dropped)
      JSON.stringify({
        type: "assistant",
        timestamp: "2026-01-01T00:00:01Z",
        message: {
          role: "assistant",
          content: [
            { type: "thinking", thinking: "hmm" },
            { type: "text", text: "We use zod." },
          ],
        },
      }),
      // dropped: no text block (tool_use only)
      JSON.stringify({
        type: "assistant",
        message: { role: "assistant", content: [{ type: "tool_use", name: "Bash", input: {} }] },
      }),
      // dropped: no text block (tool_result only)
      JSON.stringify({
        type: "user",
        message: { role: "user", content: [{ type: "tool_result", content: "..." }] },
      }),
      // dropped: isMeta
      JSON.stringify({
        type: "user",
        isMeta: true,
        message: { role: "user", content: "<system-injected>" },
      }),
      // malformed line: must not throw
      "{ not json",
      // blank line: must be skipped
      "",
    ];
    writeFileSync(file, lines.join("\n"));

    const result = readClaudeTranscript(file);

    expect(result).toEqual([
      { role: "user", content: "how do we validate input?", timestamp: "2026-01-01T00:00:00Z" },
      { role: "assistant", content: "We use zod.", timestamp: "2026-01-01T00:00:01Z" },
    ]);
  });

  it("fails open (returns []) when the file cannot be read", () => {
    expect(readClaudeTranscript(join(root, "does-not-exist.jsonl"))).toEqual([]);
  });
});
