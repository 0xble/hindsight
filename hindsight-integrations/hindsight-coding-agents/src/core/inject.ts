/** The system-prompt injection wrapper for a surfaced memory (harness-agnostic text). */

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
    "VISIBLE ATTRIBUTION — SHOW HINDSIGHT WORKING:\n" +
    "When this memory is relevant to your answer, surface it with this exact markdown header at the " +
    "top of the relevant section — a blockquote with the brain emoji, bold label, em-dash separator, " +
    "and a tight summary naming the specific facts you're drawing on:\n\n" +
    "> 🧠 **Using Hindsight Memories** — {summary of the specific facts you're drawing on}\n\n" +
    "Rules:\n" +
    "- WHEN IN DOUBT, EMIT. Over-attribution beats invisible value; if the memory is even loosely " +
    "relevant to the topic, surface it.\n" +
    "- Skip the header only when the memory is clearly unrelated to what the user asked.\n" +
    '- Name the specific facts in the summary — not a meta-statement like "using memory."\n' +
    "- One header per response is enough; place it at the top of the section that benefits from it.\n" +
    "- If the memory is relevant but WRONG or STALE, still surface it and say so explicitly " +
    '("memory said X, but the code now shows Y") so the correction is visible.\n' +
    "- Emit plain markdown only — no ANSI escape sequences, raw HTML, or color syntax.\n\n" +
    memory
  );
}
