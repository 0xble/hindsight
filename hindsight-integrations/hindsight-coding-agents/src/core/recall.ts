import type { RecallResult } from "./hindsight";
import { ATTRIBUTION_PREAMBLE } from "./attribution";

/** Leading recall framing (verbatim from the proven v1 preamble) — sits above the attribution directive. */
const RECALL_FRAMING =
  "Relevant memories from past conversations (prioritize recent when conflicting). Use any memories " +
  "that relate to what the user is asking; ignore only the ones that are clearly off-topic.";

/** Build the <hindsight_memories> system block from recalled results. Empty string when none. */
export function formatMemories(results: RecallResult[]): string {
  const items = results.map((r) => r.text.trim()).filter(Boolean);
  if (!items.length) return "";
  const body = items.map((m) => `- ${m}`).join("\n");
  return `<hindsight_memories>\n${RECALL_FRAMING}\n\n${ATTRIBUTION_PREAMBLE}\n\n${body}\n</hindsight_memories>`;
}
