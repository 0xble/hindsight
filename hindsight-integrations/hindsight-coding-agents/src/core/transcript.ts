/**
 * Claude Code session transcript (JSONL) reader: parses the raw per-line event log into normalized
 * turns for write-back. Keeps the ENGINEERING SUBSTANCE of a coding session — user requests,
 * assistant narration, AND the concrete actions it took: `tool_use` calls (name + input) and
 * `tool_result` output (truncated). A text-only transcript throws away exactly what the extractor
 * needs from a coding session (which files were edited, what commands ran, what came back), so those
 * blocks are rendered inline as markdown rather than dropped.
 *
 * Drops non-message lines (`last-prompt`, `mode`, `summary`, …), `isMeta` lines, `isSidechain`
 * (subagent/Task) lines, `thinking` blocks, and turns that render to nothing. Injected recall
 * context (`<hindsight_memories>` / `<hindsight_bank>` / `<relevant_memories>`) is stripped so the
 * write-back can't feed recalled memory back into the bank (a retain→recall feedback loop). A
 * `tool_result`-only user line is emitted with role `"tool"` (it's an environment return, not a
 * user turn). Fail-open: never throws on a missing file, malformed line, or a line that parses to a
 * non-object JSON value (`null`, a number, a boxed primitive, …).
 */
import { readFileSync } from "node:fs";
import type { TransportTurn } from "./chat";
import { stripInjectedMemory, truncate } from "./transcript-util";

interface ContentBlock {
  type?: string;
  text?: string;
  name?: string;
  input?: unknown;
  content?: string | ContentBlock[];
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

/** Compact JSON of a tool input; empty string if it can't be serialized (e.g. a cycle). */
function compactJson(v: unknown): string {
  try {
    return JSON.stringify(v) ?? "";
  } catch {
    return "";
  }
}

/** A tool_result's `content` is either a plain string or a block array — pull the text out. */
function toolResultText(content: string | ContentBlock[] | undefined): string {
  if (typeof content === "string") return content;
  if (Array.isArray(content)) {
    return content
      .filter((b) => b?.type === "text" && typeof b.text === "string")
      .map((b) => b.text as string)
      .join("\n");
  }
  return "";
}

interface RenderedLine {
  role: string;
  content: string;
}

/**
 * Render one message's `content` into a single markdown turn, or `null` if nothing usable remains.
 * Text → prose (injected-memory stripped); `tool_use` → `**Name** {compact input}`; `tool_result`
 * → `↳ output`. Tool text is truncated to `TOOL_TEXT_CAP`. A user-type line with tool output but no
 * real text is reclassified role `"tool"`.
 */
function renderLine(
  content: string | ContentBlock[] | undefined,
  type: string
): RenderedLine | null {
  if (typeof content === "string") {
    const text = stripInjectedMemory(content).trim();
    return text ? { role: type, content: text } : null;
  }
  if (!Array.isArray(content)) return null;

  const parts: string[] = [];
  let hasText = false;
  for (const b of content) {
    if (!b || typeof b !== "object") continue;
    if (b.type === "text" && typeof b.text === "string") {
      const t = stripInjectedMemory(b.text).trim();
      if (t) {
        parts.push(t);
        hasText = true;
      }
    } else if (b.type === "tool_use" && typeof b.name === "string") {
      const body = truncate(compactJson(b.input));
      parts.push(body ? `**${b.name}** ${body}` : `**${b.name}**`);
    } else if (b.type === "tool_result") {
      const t = truncate(toolResultText(b.content).trim());
      if (t) parts.push(`↳ ${t}`);
    }
  }

  const joined = parts.join("\n").trim();
  if (!joined) return null;
  // A user-type line carrying only tool output is an environment return, not a user turn.
  const role = type === "user" && !hasText ? "tool" : type;
  return { role, content: joined };
}

/** Parse a Claude Code transcript JSONL into normalized markdown turns (text + tool calls/results).
 *  Drops thinking blocks, isMeta/isSidechain lines, injected memory, and empty turns.
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

    // `type` is validated as "user" | "assistant" above; drive role from it (not the redundant
    // `message.role`). `renderLine` may reclassify a tool-output-only user line to role "tool".
    const rendered = renderLine(line.message.content, line.type);
    if (!rendered) continue;

    const turn: TransportTurn = { role: rendered.role, content: rendered.content };
    if (typeof line.timestamp === "string") turn.timestamp = line.timestamp;
    turns.push(turn);
  }

  return turns;
}
