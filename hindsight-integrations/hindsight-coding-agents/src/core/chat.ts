/**
 * Harness-agnostic chat memory: the JSON user/assistant transcript schema shared by BOTH the
 * backfill (ingest past sessions) and the live runtime write-back. A leading `system` turn carries
 * the REF-ID tracer; every turn gets an ABSOLUTE timestamp.
 */
import type { HindsightClient } from "./hindsight";
import type { ChatSession } from "./types";
import { pool } from "./util";

export interface TransportTurn {
  role: string;
  content: string;
  timestamp?: string;
}

/** Prepend the REF-ID system turn to a set of already-normalized turns. */
export function withRefId(refId: string, turns: TransportTurn[], baseTs: string): TransportTurn[] {
  return [{ role: "system", content: `REF-ID: ${refId}`, timestamp: baseTs }, ...turns];
}

/** Markdown section header per turn role. Unknown roles fall back to the raw role name. */
const ROLE_HEADING: Record<string, string> = {
  user: "User",
  assistant: "Assistant",
  tool: "Tool result",
  system: "System",
};

/**
 * Render normalized turns as a READABLE MARKDOWN transcript for the live session write-back — a
 * `## User` / `## Assistant` / `## Tool result` document led by the `REF-ID` tracer, rather than a
 * `JSON.stringify` array. The extractor reads a legible transcript better than a raw JSON blob, and
 * the turns already carry the session's tool activity (see core/transcript.ts). Each turn keeps its
 * absolute timestamp inline so the extractor can order a revised decision correctly.
 */
export function renderSessionMarkdown(
  refId: string,
  turns: TransportTurn[],
  baseTs: string
): string {
  const sections = turns.map((t) => {
    const heading = ROLE_HEADING[t.role] ?? t.role;
    const when = t.timestamp ? ` (${t.timestamp})` : "";
    return `## ${heading}${when}\n\n${t.content}`;
  });
  return [`REF-ID: ${refId}`, `_session started ${baseTs}_`, ...sections].join("\n\n");
}

/** Backfill: ingest past sessions RAW as JSON transcripts under the `chat` strategy. */
export async function ingestChats(
  client: HindsightClient,
  sessions: ChatSession[],
  opts: { concurrency?: number; log?: (m: string) => void } = {}
): Promise<number> {
  const log = opts.log ?? (() => {});
  if (!sessions.length) {
    log("[chat] no sessions; skipping");
    return 0;
  }
  log(`[chat] ingesting ${sessions.length} chats (RAW, JSON user/assistant transcript) …`);
  const NOW = Date.now(); // anchor synthesized times to a real, ABSOLUTE clock (not a fabricated epoch)
  let failures = 0;
  await pool(
    sessions,
    opts.concurrency ?? 8,
    async (s, i) => {
      const id = s.id || `s${i}`;
      // each turn gets an ABSOLUTE timestamp: its own if provided, else synthesized from the real clock,
      // staggered per session + 1 min/turn to preserve ordering. List order is CHRONOLOGICAL (a later
      // chat can amend an earlier one), so the LAST session is the newest — the previous `NOW - i*1h`
      // inverted recency and made an amendment rank older than the decision it superseded.
      const sessBase = NOW - (sessions.length - 1 - i) * 3600000;
      const baseIso = new Date(sessBase).toISOString();
      const turns = withRefId(
        `chat:${id}`,
        (s.turns || []).map((t, j) => ({
          role: t.role,
          content: t.text,
          timestamp: t.timestamp || new Date(sessBase + (j + 1) * 60000).toISOString(),
        })),
        baseIso
      );
      await client.retain(
        JSON.stringify(turns),
        "developer chat",
        `chat:${id}`,
        ["source:chat"],
        "chat",
        {
          timestamp: baseIso,
          metadata: { source: "chat", chat: id, ref_id: `chat:${id}` },
        }
      );
    },
    (i, e) => {
      failures++;
      log(`  ! chat ${i} failed to enqueue: ${(e as Error).message?.slice(0, 120)}`);
    }
  );
  log(`[chat] done: ${sessions.length} chats ingested (JSON) under strategy 'chat'`);
  return failures;
}

/**
 * Live write-back: upsert a running session under a stable document_id. Same id => Hindsight
 * reprocesses the FULL conversation, so the settled decision is extracted from the whole thing.
 *
 * Uses the `session` strategy (verbose extraction), NOT `chat` (the ≤2-fact custom extractor tuned
 * for short backfilled decision logs) — a live work session makes several durable decisions/changes
 * and would be gutted by ≤2-fact extraction. The content is a readable markdown transcript that
 * includes the session's tool activity (see core/transcript.ts + renderSessionMarkdown).
 */
export async function retainLiveSession(
  client: HindsightClient,
  sessionId: string,
  turns: TransportTurn[],
  startTs: string
): Promise<void> {
  const refId = `conversation:${sessionId}`;
  await client.retain(
    renderSessionMarkdown(refId, turns, startTs),
    "coding agent session",
    refId,
    ["source:chat"],
    "session",
    {
      timestamp: startTs,
      async: true,
      metadata: { source: "chat", session_id: sessionId, ref_id: refId },
    }
  );
}
