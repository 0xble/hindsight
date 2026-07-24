/** Shared visible-attribution directive: instructs the agent to surface a recognizable
 *  header when it uses recalled memory. Reused by both the reflect injection (inject.ts)
 *  and per-turn recall formatting (recall.ts). */
export const ATTRIBUTION_PREAMBLE =
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
  "- Emit plain markdown only — no ANSI escape sequences, raw HTML, or color syntax.";
