/** The system-prompt injection wrapper for a surfaced memory (harness-agnostic text). */

export function buildSystemInjection(memory: string): string {
  return (
    "Relevant project memory, surfaced from THIS repository's git history and past developer " +
    "conversations — a past decision that likely explains this issue. If it states an EXACT rule " +
    "or literal values (specific strings, numbers, set members, mappings), apply them PRECISELY as " +
    "given — do not substitute a different but plausible choice of your own. " +
    "Verify against the current code before editing. " +
    "ATTRIBUTION: when this memory informs your answer or fix, say so VISIBLY — introduce that " +
    "part with \"🧠 From Hindsight memory:\" so the user can tell which insight came from the " +
    "project's memory rather than from the current code. Never claim it when it did not " +
    "contribute:\n\n" +
    memory
  );
}
