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

/** SessionStart: teach when/why to use pages + list what exists. Empty-state aware. */
export function buildKnowledgePreamble(pages: PageRef[]): string {
  const body = pages.length
    ? `Knowledge pages available in this repository:\n${roster(pages)}`
    : "No knowledge pages yet — Hindsight is still learning this repo; they'll appear as it processes.";
  return (
    "<hindsight_knowledge>\n" +
    "This repository has a Hindsight knowledge base: curated, continuously-updated pages summarizing its " +
    "durable engineering knowledge (architecture, components, conventions, key decisions, and in-flight initiatives).\n" +
    "Before substantial work, consult the relevant pages instead of re-deriving understanding from the code: read " +
    "Conventions before writing new code, the Component map before changing a subsystem, and an initiative's page " +
    "before continuing that feature.\n" +
    `${body}\n` +
    "Read one with hindsight_read_knowledge_page(page_id). Follow any [[page:<id>]] links you see. The list is " +
    "re-injected for you periodically as it changes.\n" +
    "CAPTURE new work: when you start (or make substantial progress on) a MAJOR new feature, initiative, or " +
    "enhancement, call hindsight_capture_initiative(title, summary) so it becomes a tracked page future sessions can " +
    "pick up. Do this for real features worth remembering across sessions — NOT for routine bug fixes, small tweaks, " +
    "or chores.\n" +
    "</hindsight_knowledge>"
  );
}

/** Periodic UserPromptSubmit refresh — compact, or undefined when there's nothing to show. */
export function buildRosterRefresh(pages: PageRef[]): string | undefined {
  if (!pages.length) return undefined;
  return (
    "<hindsight_knowledge_refresh>\n" +
    `Current Hindsight knowledge pages (may have changed):\n${roster(pages)}\n` +
    "Read any with hindsight_read_knowledge_page(page_id). If you've begun a major new feature/initiative this " +
    "session and haven't recorded it, call hindsight_capture_initiative(title, summary).\n" +
    "</hindsight_knowledge_refresh>"
  );
}
