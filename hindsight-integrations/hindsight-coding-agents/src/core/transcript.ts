/**
 * Claude Code session transcript (JSONL) reader: parses the raw per-line event log into
 * normalized user/assistant text turns for write-back. Drops non-message lines (`last-prompt`,
 * `mode`, `summary`, …), `isMeta` lines, `isSidechain` (subagent/Task) lines,
 * thinking/tool_use/tool_result blocks, and turns whose extracted text is empty. Fail-open: never
 * throws on a missing file, malformed line, or a line that parses to a non-object JSON value
 * (`null`, a number, a boxed primitive, …).
 */
import { readFileSync } from "node:fs";
import type { TransportTurn } from "./chat";

interface ContentBlock {
  type?: string;
  text?: string;
}

interface TranscriptLine {
  type?: string;
  isMeta?: boolean;
  isSidechain?: boolean;
  timestamp?: string;
  message?: {
    content?: string | ContentBlock[];
  };
}

/** Extract plain text from a Claude message's `content` (string, or block array — text blocks only). */
function extractText(content: string | ContentBlock[] | undefined): string {
  if (typeof content === "string") return content.trim();
  if (Array.isArray(content)) {
    return content
      .filter((b) => b?.type === "text" && typeof b.text === "string")
      .map((b) => b.text as string)
      .join("\n")
      .trim();
  }
  return "";
}

/** Parse a Claude Code transcript JSONL into normalized user/assistant text turns.
 *  Drops tool_use/tool_result/thinking blocks, isMeta/isSidechain lines, and empty turns.
 *  Never throws on bad lines. */
export function readClaudeTranscript(path: string): TransportTurn[] {
  let raw: string;
  try {
    raw = readFileSync(path, "utf8");
  } catch {
    return [];
  }

  const turns: TransportTurn[] = [];
  for (const rawLine of raw.split("\n")) {
    const trimmed = rawLine.trim();
    if (!trimmed) continue;

    let parsed: unknown;
    try {
      parsed = JSON.parse(trimmed);
    } catch {
      continue;
    }
    // JSON.parse accepts non-object top-level values (`null`, numbers, booleans, arrays);
    // guard here so a corrupt/truncated line can't reach a property access below and throw.
    if (typeof parsed !== "object" || parsed === null) continue;
    const line = parsed as TranscriptLine;

    if (line.type !== "user" && line.type !== "assistant") continue;
    if (line.isMeta === true) continue;
    if (line.isSidechain === true) continue;
    if (typeof line.message !== "object" || line.message === null) continue;

    const content = extractText(line.message.content);
    if (!content) continue;

    // `type` is already validated as "user" | "assistant" above; reuse it as the role instead
    // of trusting `message.role`, which is a second, redundant source of the same value.
    const turn: TransportTurn = { role: line.type, content };
    if (typeof line.timestamp === "string") turn.timestamp = line.timestamp;
    turns.push(turn);
  }

  return turns;
}
