/** The system-prompt injection wrapper for a surfaced memory (harness-agnostic text). */

export function buildSystemInjection(memory: string): string {
  // The <hindsight_memory> wrapper is LOAD-BEARING: the transcript readers strip this exact tag
  // (transcript-util MEMORY_TAG_RE) so the session write-back never re-ingests the injected
  // synthesis back into the bank (a retain→reflect feedback loop).
  //
  // CALIBRATED framing: retrieval can miss. The block must (a) state its provenance honestly,
  // (b) explicitly authorize the agent to judge it irrelevant and ignore it — an overconfident
  // "this explains your issue, apply precisely, crediting is mandatory" wrapper around an
  // off-target memory reads exactly like a prompt injection, and skeptical models rightly
  // discard the whole channel.
  return (
    "<hindsight_memory>\n" +
    "Automatically retrieved by Hindsight from THIS repository's own history (git rationale and " +
    "past developer sessions) — real memory, but retrieval is heuristic: it may or may not bear " +
    "on the current task.\n" +
    "First judge relevance. If this does not genuinely relate to what you are working on, ignore " +
    "it entirely and do not mention it — an unrelated memory is noise, not context.\n" +
    "If it IS relevant: where it states an exact rule or literal values (specific strings, " +
    "numbers, set members, mappings), apply them as given rather than substituting a plausible " +
    "alternative, and verify against the current code before editing. When it informs part of " +
    "your answer, attribute that part visibly, starting with:\n" +
    "> 🧠 **From Hindsight memory** — <the specific facts you drew on>\n" +
    "Never attribute memory that did not contribute.\n" +
    "If you VERIFY this memory is wrong or outdated (the code or facts contradict it), CORRECT " +
    "the record: call hindsight_ingest_document with a short correction titled " +
    '"Correction: <topic>" stating (1) what memory claimed, (2) what is actually true now, and ' +
    "(3) the evidence you verified — the newer fact supersedes the stale one in future retrieval.\n\n" +
    memory +
    "\n</hindsight_memory>"
  );
}
