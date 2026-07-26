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
 * identically: user text (real prompts), assistant text, `**name** {args}` for calls, `↳ name: output`
 * for results (role "tool"). Gemini's synthetic `<session_context>` bootstrap message (project tree +
 * environment) is dropped — that's agent setup, not the user's work — and stripInjectedMemory is a
 * defensive second pass so a retain never feeds our own injected memory back into recall. Fail-open:
 * never throws on a missing file or a malformed line.
 */
import { readFileSync } from "node:fs";
import type { TransportTurn } from "./chat";
import { stripInjectedMemory, truncate } from "./transcript-util";

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

/** Compact JSON of a tool input; empty string if it can't be serialized (e.g. a cycle). */
function compactJson(v: unknown): string {
  try {
    return JSON.stringify(v) ?? "";
  } catch {
    return "";
  }
}

/** A functionResponse's `response` is usually `{ output: string }`; fall back to serializing it. */
function responseText(response: unknown): string {
  if (
    response &&
    typeof response === "object" &&
    "output" in (response as Record<string, unknown>)
  ) {
    const o = (response as Record<string, unknown>).output;
    return typeof o === "string" ? o : compactJson(o);
  }
  return typeof response === "string" ? response : compactJson(response);
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

/** Render one reconstructed Gemini message into a single normalized turn, or null if nothing usable. */
function renderMessage(m: GeminiMessage): TransportTurn | null {
  const role = m.type;
  if (role !== "user" && role !== "gemini") return null; // drop system/other roles

  // Drop the synthetic session-context bootstrap (agent setup, not user work).
  if (role === "user" && isSyntheticUserText(firstText(m.content))) return null;

  // Assistant text is usually a plain string.
  if (typeof m.content === "string") {
    const t = stripInjectedMemory(m.content).trim();
    if (!t) return null;
    return { role: role === "gemini" ? "assistant" : "user", content: t };
  }
  if (!Array.isArray(m.content)) return null;

  const parts: string[] = [];
  let sawText = false;
  let sawToolResponse = false;
  for (const p of m.content) {
    if (!p || typeof p !== "object") continue;
    if (typeof p.text === "string") {
      const t = stripInjectedMemory(p.text).trim();
      if (t) {
        parts.push(t);
        sawText = true;
      }
    } else if (p.functionCall && typeof p.functionCall.name === "string") {
      const body = truncate(compactJson(p.functionCall.args));
      parts.push(body ? `**${p.functionCall.name}** ${body}` : `**${p.functionCall.name}**`);
    } else if (p.functionResponse && typeof p.functionResponse.name === "string") {
      const out = truncate(responseText(p.functionResponse.response).trim());
      parts.push(out ? `↳ ${p.functionResponse.name}: ${out}` : `↳ ${p.functionResponse.name}`);
      sawToolResponse = true;
    }
  }

  const joined = parts.join("\n").trim();
  if (!joined) return null;
  // A user message carrying only tool responses is an environment return, not a user turn.
  const outRole =
    role === "user" && sawToolResponse && !sawText
      ? "tool"
      : role === "gemini"
        ? "assistant"
        : "user";
  return { role: outRole, content: joined };
}

/** Parse a Gemini CLI session JSONL (mutation log) into normalized markdown turns.
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
  const turns: TransportTurn[] = [];
  for (const m of finalMessages) {
    const t = renderMessage(m);
    if (t) turns.push(t);
  }
  return turns;
}
