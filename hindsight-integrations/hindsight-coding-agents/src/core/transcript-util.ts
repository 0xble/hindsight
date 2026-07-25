/** Shared transcript-rendering helpers used by both the Claude (transcript.ts) and Codex
 *  (transcript-codex.ts) session readers. */

/** Cap on any single rendered tool input or tool result (mirrors v1's 2000-char cap): small
 *  edits/commands are captured verbatim while a giant Write/output is bounded. */
export const TOOL_TEXT_CAP = 2000;

/** Injected recall/knowledge context — stripped from retained text so a write-back never re-ingests
 *  its own injected memory (a retain→recall feedback loop). Covers every block the hooks inject. */
const MEMORY_TAG_RE =
  /<(hindsight_memories|hindsight_bank|relevant_memories|user_feedback|hindsight_knowledge|hindsight_knowledge_refresh)\b[\s\S]*?<\/\1>/g;

export function stripInjectedMemory(s: string): string {
  return s.replace(MEMORY_TAG_RE, "");
}

export function truncate(s: string, max = TOOL_TEXT_CAP): string {
  return s.length > max ? `${s.slice(0, max)}… (truncated)` : s;
}
