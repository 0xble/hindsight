/**
 * Claude Code session transcript (JSONL) reader: parses the raw per-line event log into
 * normalized user/assistant text turns for write-back. Drops non-message lines (`last-prompt`,
 * `mode`, `summary`, …), `isMeta` lines, thinking/tool_use/tool_result blocks, and turns whose
 * extracted text is empty. Fail-open: never throws on a missing file or malformed line.
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
  timestamp?: string;
  message?: {
    role?: string;
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
 *  Drops tool_use/tool_result/thinking blocks, isMeta lines, and empty turns. Never throws on bad lines. */
export function readClaudeTranscript(path: string): TransportTurn[] {
  let raw: string;
  try {
    raw = readFileSync(path, "utf8");
  } catch {
    return [];
  }

  const turns: TransportTurn[] = [];
  for (const line of raw.split("\n")) {
    const trimmed = line.trim();
    if (!trimmed) continue;

    let parsed: TranscriptLine;
    try {
      parsed = JSON.parse(trimmed);
    } catch {
      continue;
    }

    if (parsed.type !== "user" && parsed.type !== "assistant") continue;
    if (parsed.isMeta === true) continue;

    const role = parsed.message?.role;
    if (typeof role !== "string") continue;

    const content = extractText(parsed.message?.content);
    if (!content) continue;

    const turn: TransportTurn = { role, content };
    if (typeof parsed.timestamp === "string") turn.timestamp = parsed.timestamp;
    turns.push(turn);
  }

  return turns;
}
