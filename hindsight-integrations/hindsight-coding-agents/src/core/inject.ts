/** The system-prompt injection wrapper for a surfaced memory (harness-agnostic text). */

export function buildSystemInjection(memory: string): string {
  return (
    "Relevant project memory, surfaced from THIS repository's git history and past developer " +
    "conversations — a past decision that likely explains this issue.\n" +
    "ATTRIBUTION REQUIREMENT (mandatory output format): when this memory informs your answer or " +
    "fix, that part of your answer MUST begin with the line:\n" +
    "🧠 From Hindsight memory:\n" +
    "Using it without the marker is an incorrect answer; crediting it when it did not contribute " +
    "is equally incorrect.\n" +
    "If it states an EXACT rule or literal values (specific strings, numbers, set members, " +
    "mappings), apply them PRECISELY as given — do not substitute a different but plausible " +
    "choice of your own. Verify against the current code before editing:\n\n" +
    memory
  );
}
