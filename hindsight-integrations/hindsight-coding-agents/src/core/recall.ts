import type { RecallResult } from "./hindsight";
import { ATTRIBUTION_PREAMBLE } from "./attribution";

/** Build the <hindsight_memories> system block from recalled results. Empty string when none. */
export function formatMemories(results: RecallResult[]): string {
  const items = results.map((r) => r.text.trim()).filter(Boolean);
  if (!items.length) return "";
  const body = items.map((m) => `- ${m}`).join("\n");
  return `<hindsight_memories>\n${ATTRIBUTION_PREAMBLE}\n\n${body}\n</hindsight_memories>`;
}
