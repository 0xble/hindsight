/**
 * opencode live-transcript normalizer.
 *
 * opencode delivers the running conversation to a plugin as an in-memory message list (via
 * `experimental.chat.messages.transform`), NOT a JSONL file like Claude/Codex — so this is a pure
 * function over that list, not a file reader. It produces the SAME rich `TransportTurn[]` shape as
 * transcript.ts / transcript-codex.ts (text + tool calls + their inline output), and reuses the
 * shared `stripInjectedMemory`/`truncate` helpers so a retain never feeds injected memory back into
 * recall. Unlike Claude (tool results arrive as a separate later message) opencode carries a tool's
 * output inline on its `ToolPart.state`, so one assistant message renders as one turn.
 */
import type { TransportTurn } from "./chat";
import { stripInjectedMemory, truncate } from "./transcript-util";

/** Structural subset of the opencode SDK's ToolState we render (avoids a hard dep on its types). */
interface OcToolState {
  status?: string;
  input?: unknown;
  output?: string; // present on status "completed"
  error?: string; // present on status "error"
}

/** Structural subset of an opencode message Part (TextPart | ToolPart | others we drop). */
export interface OcPart {
  type?: string;
  text?: string; // TextPart
  tool?: string; // ToolPart: the tool name
  state?: OcToolState; // ToolPart: call input + inline output/error
}

/** Structural subset of an opencode message ({ info, parts }). */
export interface OcMessage {
  info?: { role?: string; sessionID?: string; time?: { created?: number } };
  parts?: OcPart[];
}

/** Compact JSON of a tool input; empty string if it can't be serialized (e.g. a cycle). */
function compactJson(v: unknown): string {
  try {
    return JSON.stringify(v) ?? "";
  } catch {
    return "";
  }
}

/**
 * Render one opencode message's parts into a single markdown turn, or null if nothing usable remains.
 * Text → prose (injected-memory stripped); a tool part → `**tool** {compact input}` followed by
 * `↳ output` (or `↳ error`). Tool text is truncated to the shared cap. reasoning/step/snapshot parts
 * are dropped. Non-conversational roles (system/tool-only) are dropped.
 */
function renderMessage(m: OcMessage): TransportTurn | null {
  const role = m.info?.role;
  if (role !== "user" && role !== "assistant") return null;

  const parts: string[] = [];
  for (const p of m.parts || []) {
    if (!p || typeof p !== "object") continue;
    if (p.type === "text" && typeof p.text === "string") {
      const t = stripInjectedMemory(p.text).trim();
      if (t) parts.push(t);
    } else if (p.type === "tool" && typeof p.tool === "string") {
      const st = p.state || {};
      const body = truncate(compactJson(st.input));
      parts.push(body ? `**${p.tool}** ${body}` : `**${p.tool}**`);
      const out = st.status === "error" ? st.error : st.output;
      const t = typeof out === "string" ? truncate(out.trim()) : "";
      if (t) parts.push(`↳ ${t}`);
    }
    // reasoning / step-start / step-finish / snapshot / patch / …: dropped
  }

  const joined = parts.join("\n").trim();
  if (!joined) return null;
  const created = m.info?.time?.created;
  return {
    role,
    content: joined,
    ...(created ? { timestamp: new Date(created).toISOString() } : {}),
  };
}

/**
 * Normalize opencode's live message list into rich transcript turns (user/assistant text plus tool
 * calls and their inline output). Never throws on malformed entries.
 */
export function readOpencodeMessages(messages: OcMessage[]): TransportTurn[] {
  const turns: TransportTurn[] = [];
  for (const m of messages || []) {
    const turn = renderMessage(m);
    if (turn) turns.push(turn);
  }
  return turns;
}

/** The session id carried on the messages (first message that has one), or undefined. */
export function opencodeSessionId(messages: OcMessage[]): string | undefined {
  return (messages || []).find((m) => m.info?.sessionID)?.info?.sessionID;
}
