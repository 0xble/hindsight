/**
 * Gemini CLI transcript reader — the Gemini counterpart to transcript.ts (Claude) and
 * transcript-codex.ts (Codex). Gemini CLI (0.52.0+) persists a session as a JSONL **mutation log**
 * at the `transcript_path` the SessionEnd hook receives (`~/.gemini/tmp/<project>/chats/session-*.jsonl`):
 *   - line 1: a header `{ kind: "main", sessionId, projectHash, startTime, lastUpdated }`
 *   - an initial `{ "$set": { "messages": [ … ] } }` seeding the messages array
 *   - then each message APPENDED as its own line `{ id, type, content, timestamp, … }`
 *   - interleaved `{ "$set": { "lastUpdated": … } }` metadata bumps (ignored)
 * Messages stream: the SAME `id` reappears on later lines with updated content, so we upsert by id
 * (first-seen position, last-seen content wins).
 *
 * Gemini's message shape is its native Content, and `content` is polymorphic:
 *   - a user PROMPT: `content` is `[{ text }]`
 *   - an assistant message (`type: "gemini"`): `content` is a plain STRING (the answer); `thoughts`
 *     carries reasoning — dropped, like Claude `thinking` / Codex `reasoning`
 *   - a tool RESULT: a `user` message whose `content` is `[{ functionResponse: { name, response } }]`
 *   - a tool CALL (when present as a part): `[{ functionCall: { name, args } }]`
 *
 * Normalized to the SAME `TransportTurn[]` shape as the other readers so live write-back renders
 * identically: user text (real prompts), assistant text, and a compact `role:"action"` turn per
 * functionCall (tool name + primary target, no args); functionResponse results are dropped. Gemini's synthetic `<session_context>` bootstrap message (project tree +
 * environment) is dropped — that's agent setup, not the user's work — and stripInjectedMemory is a
 * defensive second pass so a retain never feeds our own injected memory back into recall. Fail-open:
 * never throws on a missing file or a malformed line.
 */
import { readFileSync } from "node:fs";
import type { TransportTurn } from "./chat";
import { actionLine, stripInjectedMemory } from "./transcript-util";

interface Part {
  text?: string;
  functionCall?: { name?: string; args?: unknown };
  functionResponse?: { name?: string; response?: unknown };
}
interface GeminiMessage {
  id?: string;
  type?: string; // "user" | "gemini"
  content?: string | Part[];
}
interface Line {
  kind?: string;
  type?: string;
  id?: string;
  content?: string | Part[];
  ["$set"]?: { messages?: GeminiMessage[] };
}

/** Gemini seeds each session with a synthetic user message (project tree + environment). Retaining
 *  it teaches the bank about the workspace layout, not the user's work — drop it. */
function isSyntheticUserText(text: string): boolean {
  return text.trimStart().startsWith("<session_context>");
}

/** The first text part of a message's content (for the synthetic-message check), or "". */
function firstText(content: string | Part[] | undefined): string {
  if (typeof content === "string") return content;
  if (Array.isArray(content)) return content.find((p) => typeof p?.text === "string")?.text ?? "";
  return "";
}

/** Render one reconstructed Gemini message into normalized turns (prose + compact action turns). */
function renderMessage(m: GeminiMessage): TransportTurn[] {
  const role = m.type;
  if (role !== "user" && role !== "gemini") return []; // drop system/other roles
  const outRole = role === "gemini" ? "assistant" : "user";

  // Drop the synthetic session-context bootstrap (agent setup, not user work).
  if (role === "user" && isSyntheticUserText(firstText(m.content))) return [];

  // Assistant text is usually a plain string.
  if (typeof m.content === "string") {
    const t = stripInjectedMemory(m.content).trim();
    return t ? [{ role: outRole, content: t }] : [];
  }
  if (!Array.isArray(m.content)) return [];

  const texts: string[] = [];
  const actions: TransportTurn[] = [];
  for (const p of m.content) {
    if (!p || typeof p !== "object") continue;
    if (typeof p.text === "string") {
      const t = stripInjectedMemory(p.text).trim();
      if (t) texts.push(t);
    } else if (p.functionCall && typeof p.functionCall.name === "string") {
      actions.push({
        role: "action",
        content: actionLine(p.functionCall.name, p.functionCall.args),
      });
    }
    // functionResponse: dropped — outputs are mechanical noise for extraction
  }

  const out: TransportTurn[] = [];
  const joined = texts.join("\n").trim();
  if (joined) out.push({ role: outRole, content: joined });
  out.push(...actions);
  return out;
}

/** Parse a Gemini CLI session JSONL (mutation log) into normalized turns.
 *  Reconstructs the messages array (upsert-by-id), drops the synthetic session-context message,
 *  reasoning, injected memory, and empty turns. Never throws on bad lines. */
export function readGeminiTranscript(path: string): TransportTurn[] {
  let raw: string;
  try {
    raw = readFileSync(path, "utf8");
  } catch {
    return [];
  }

  // Reconstruct the messages array from the mutation log: `$set.messages` replaces it wholesale;
  // an appended message record upserts by id (first-seen order, last-seen content wins).
  const order: string[] = [];
  const byId = new Map<string, GeminiMessage>();
  const noId: GeminiMessage[] = [];

  const upsert = (m: GeminiMessage) => {
    if (typeof m.id === "string") {
      if (!byId.has(m.id)) order.push(m.id);
      byId.set(m.id, m);
    } else {
      noId.push(m);
    }
  };
  const reset = (msgs: GeminiMessage[]) => {
    order.length = 0;
    byId.clear();
    noId.length = 0;
    for (const m of msgs) if (m && typeof m === "object") upsert(m);
  };

  for (const rawLine of raw.split("\n")) {
    const trimmed = rawLine.trim();
    if (!trimmed) continue;
    let obj: Line;
    try {
      obj = JSON.parse(trimmed) as Line;
    } catch {
      continue;
    }
    if (!obj || typeof obj !== "object") continue;
    if (obj["$set"]) {
      if (Array.isArray(obj["$set"].messages)) reset(obj["$set"].messages);
      continue; // other $set ops (e.g. lastUpdated) carry no conversation
    }
    // A message record has a role (`type`) and content; the header (kind:"main") has neither.
    if (typeof obj.type === "string" && obj.content !== undefined) upsert(obj as GeminiMessage);
  }

  const finalMessages = [...order.map((id) => byId.get(id) as GeminiMessage), ...noId];
  return finalMessages.flatMap((m) => renderMessage(m));
}
