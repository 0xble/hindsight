export interface PageRef {
  id: string;
  title: string;
}

/** Defensive parse of HindsightClient.listPages() (GET /mental-models?detail=metadata → {items:[{id,name}]}). */
export function parsePageList(raw: unknown): PageRef[] {
  const items = (raw as { items?: unknown })?.items;
  if (!Array.isArray(items)) return [];
  const out: PageRef[] = [];
  for (const it of items) {
    const id = (it as { id?: unknown })?.id;
    const name = (it as { name?: unknown })?.name;
    if (typeof id === "string" && typeof name === "string") out.push({ id, title: name });
  }
  return out;
}

function roster(pages: PageRef[]): string {
  return pages.map((p) => `- ${p.title} (${p.id})`).join("\n");
}

/**
 * When-to-call guide for the FULL Hindsight tool suite. Shared by the SessionStart preamble and the
 * periodic refresh so the agent is told — repeatedly — not just that the tools exist but the moment
 * to reach for each one. Registering the tools isn't enough; the trigger for each has to be in
 * context. (Omits hindsight_get_current_bank — pure introspection, no workflow trigger.)
 */
const TOOL_GUIDE =
  "- hindsight_search_knowledge_pages(query) — FIRST STOP for any question the project's accumulated " +
  "knowledge might answer (components, conventions, past decisions, initiatives): search the knowledge " +
  'pages and credit results visibly with "🧠 From Hindsight memory (<page>):".\n' +
  "- hindsight_list_knowledge_pages / hindsight_read_knowledge_page — BEFORE substantial work, list the pages and " +
  "read the relevant ones to ground yourself in this repo's architecture, conventions, and past decisions instead " +
  "of re-deriving them from the code; follow any [[page:<id>]] links you see.\n" +
  "- hindsight_reflect(query) — when pages are too shallow and you need the WHY: deep reasoning over the " +
  "repo's full memory for the past decision and exact values that explain a behavior or bug (slower — " +
  'use deliberately, and credit results with "🧠 From Hindsight memory:").\n' +
  "- hindsight_capture_initiative(title, summary) — right after the user approves a plan or finishes brainstorming a " +
  "new feature/capability and you are about to start implementing (BEFORE you write any code), call this ONCE to " +
  "record it as a tracked page. Skip bug fixes, small tweaks, and chores.\n" +
  "- hindsight_ingest_document(title, content) — save an external document or durable notes/findings you want " +
  "remembered (not the current conversation — that is captured automatically at session end).";

/** SessionStart: teach the whole tool suite + when to use each, and list what pages exist. Empty-state aware. */
export function buildKnowledgePreamble(pages: PageRef[]): string {
  const body = pages.length
    ? `Knowledge pages currently in this repository:\n${roster(pages)}`
    : "No knowledge pages yet — Hindsight is still learning this repo; they'll appear as it processes.";
  return (
    "<hindsight_knowledge>\n" +
    "This repository has a Hindsight memory + knowledge base (curated, continuously-updated pages plus the raw " +
    "memory behind them). The tools below are registered, but you must actually CALL them at the right moments:\n" +
    `${TOOL_GUIDE}\n` +
    `${body}\n` +
    "This tool guide and the page list are re-injected for you periodically as things change.\n" +
    "</hindsight_knowledge>"
  );
}

/**
 * Periodic UserPromptSubmit refresh. ALWAYS emits (never undefined) so the full tool guide keeps
 * re-appearing in context even on a fresh repo with no pages yet — precisely when the agent is
 * building its first features. The page roster is included only when pages exist; the reminder of
 * which tools exist and WHEN to call each is unconditional.
 */
export function buildRosterRefresh(pages: PageRef[]): string {
  const rosterBlock = pages.length
    ? `Current Hindsight knowledge pages (may have changed):\n${roster(pages)}\n`
    : "";
  return (
    "<hindsight_knowledge_refresh>\n" +
    rosterBlock +
    "Reminder — this repo's Hindsight tools are available; call them at the right moments:\n" +
    `${TOOL_GUIDE}\n` +
    "</hindsight_knowledge_refresh>"
  );
}
