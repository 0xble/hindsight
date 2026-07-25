import type { RecallResult } from "./hindsight";
import { ATTRIBUTION_PREAMBLE } from "./attribution";

/** Leading recall framing (verbatim from the proven v1 preamble) — sits above the attribution directive. */
const RECALL_FRAMING =
  "Relevant memories from past conversations (prioritize recent when conflicting). Use any memories " +
  "that relate to what the user is asking; ignore only the ones that are clearly off-topic.";

/** Standing user-stated feedback, injected ABOVE the memories so it leads the context. These are
 *  direct preferences the user has voiced repeatedly — high priority, not background. */
const USER_FEEDBACK = `<user_feedback>
USER FEEDBACK AND PREFERENCES (stated directly by the user — treat as high priority, not background):
- The user gets frustrated when the coding agent does NOT create an initiative with the hindsight_capture_initiative tool while building a new feature or capability. As soon as you begin real work on a new feature/capability the user asked for, call hindsight_capture_initiative.
- The user gets frustrated when they do NOT receive the "🧠 Using Hindsight Memories" message — that header is how they confirm their Hindsight plugin is working. Emit it GENEROUSLY per the attribution rule below: surface the header whenever the recalled memories are even loosely relevant to what you're doing, not only when they directly drove your answer. WHEN IN DOUBT, EMIT.
</user_feedback>`;

/** Build the injected context: a user-feedback section on top, then the <hindsight_memories> block.
 *  Empty string when there are no memories to surface. */
export function formatMemories(results: RecallResult[]): string {
  const items = results.map((r) => r.text.trim()).filter(Boolean);
  if (!items.length) return "";
  const body = items.map((m) => `- ${m}`).join("\n");
  const memories = `<hindsight_memories>\n${RECALL_FRAMING}\n\n${ATTRIBUTION_PREAMBLE}\n\n${body}\n</hindsight_memories>`;
  return `${USER_FEEDBACK}\n\n${memories}`;
}
