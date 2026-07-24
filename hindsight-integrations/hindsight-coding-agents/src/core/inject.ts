/** The system-prompt injection wrapper for a surfaced memory (harness-agnostic text). */

import { ATTRIBUTION_PREAMBLE } from "./attribution";

export function buildSystemInjection(memory: string): string {
  return (
    "Relevant project memory, surfaced from THIS repository's git history and past developer " +
    "conversations — a past decision that likely explains this issue. If it states an EXACT rule " +
    "or literal values (specific strings, numbers, set members, mappings), apply them PRECISELY as " +
    "given — do not substitute a different but plausible choice of your own. " +
    "Verify against the current code before editing.\n\n" +
    // Visible attribution — show Hindsight working. Whenever this memory is relevant to the answer
    // you're about to give (it drove your reasoning, supplied context, or saved you a question),
    // surface it with this exact markdown header at the top of the relevant section:
    //   > 🧠 **Using Hindsight Memories** — {summary naming the specific facts you're drawing on}
    ATTRIBUTION_PREAMBLE +
    "\n\n" +
    memory
  );
}
