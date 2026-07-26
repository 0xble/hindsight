import { describe, expect, it } from "vitest";
import { readOpencodeMessages, opencodeSessionId, type OcMessage } from "./transcript-opencode";

describe("readOpencodeMessages", () => {
  it("keeps user/assistant text + tool calls with inline output; drops other roles/parts", () => {
    const messages: OcMessage[] = [
      // non-conversational role: dropped
      { info: { role: "system" }, parts: [{ type: "text", text: "you are opencode" }] },
      // real user prompt: kept
      {
        info: { role: "user", sessionID: "ses_1", time: { created: 1_700_000_000_000 } },
        parts: [{ type: "text", text: "add retry backoff to the uploader" }],
      },
      // assistant message: text + a completed tool call (input + inline output) + a dropped reasoning part
      {
        info: { role: "assistant" },
        parts: [
          { type: "reasoning", text: "thinking…" },
          { type: "text", text: "I'll add exponential backoff." },
          {
            type: "tool",
            tool: "bash",
            state: { status: "completed", input: { cmd: "npm test" }, output: "12 passed" },
          },
        ],
      },
      // assistant message with an errored tool call: renders the error after the call
      {
        info: { role: "assistant" },
        parts: [
          {
            type: "tool",
            tool: "read",
            state: { status: "error", input: { path: "nope.ts" }, error: "ENOENT" },
          },
        ],
      },
    ];

    expect(readOpencodeMessages(messages)).toEqual([
      {
        role: "user",
        content: "add retry backoff to the uploader",
        timestamp: new Date(1_700_000_000_000).toISOString(),
      },
      {
        role: "assistant",
        content: 'I\'ll add exponential backoff.\n**bash** {"cmd":"npm test"}\n↳ 12 passed',
      },
      { role: "assistant", content: '**read** {"path":"nope.ts"}\n↳ ENOENT' },
    ]);
  });

  it("strips injected memory that leaks into a kept message", () => {
    const messages: OcMessage[] = [
      {
        info: { role: "user" },
        parts: [
          {
            type: "text",
            text: "<hindsight_memories>\nleak\n</hindsight_memories>\nWhy retry?",
          },
        ],
      },
    ];
    expect(readOpencodeMessages(messages)).toEqual([{ role: "user", content: "Why retry?" }]);
  });

  it("truncates a very long tool output to the shared cap", () => {
    const messages: OcMessage[] = [
      {
        info: { role: "assistant" },
        parts: [
          {
            type: "tool",
            tool: "bash",
            state: { status: "completed", input: {}, output: "x".repeat(5000) },
          },
        ],
      },
    ];
    const turns = readOpencodeMessages(messages);
    expect(turns).toHaveLength(1);
    expect(turns[0].content).toContain("… (truncated)");
    expect(turns[0].content.length).toBeLessThan(2100);
  });

  it("drops a message with no usable parts, and tolerates a missing parts array", () => {
    const messages: OcMessage[] = [
      { info: { role: "assistant" }, parts: [{ type: "step-start" }] }, // no text/tool: dropped
      { info: { role: "user" } }, // no parts: tolerated, dropped
    ];
    expect(readOpencodeMessages(messages)).toEqual([]);
  });

  it("opencodeSessionId returns the first message's session id, or undefined", () => {
    expect(
      opencodeSessionId([
        { info: { role: "assistant" } },
        { info: { role: "user", sessionID: "ses_9" } },
      ])
    ).toBe("ses_9");
    expect(opencodeSessionId([{ info: { role: "user" } }])).toBeUndefined();
    expect(opencodeSessionId([])).toBeUndefined();
  });
});
