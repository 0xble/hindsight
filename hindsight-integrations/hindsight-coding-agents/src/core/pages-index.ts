/**
 * Local knowledge-page section index — the per-turn injection surface.
 *
 * Pages are the per-turn memory: pre-synthesized by the server ("organized like reflect"), matched
 * locally per prompt ("fast like recall") — no server round-trip, no LLM call, ~ms per turn.
 *
 * Mechanics: each page's markdown is split at headings into SECTIONS; a prompt is scored against
 * sections by weighted term overlap (heading hits count extra); the top sections are injected,
 * trimmed to a token budget, each with provenance and a pointer to the full page. Below a score
 * floor nothing is injected — silence over noise.
 */

export interface PageContent {
  id: string;
  title: string;
  content: string;
}

export interface PageSection {
  pageId: string;
  pageTitle: string;
  heading: string;
  body: string;
  terms: Map<string, number>; // term -> weight (heading terms weighted)
}

const STOP = new Set(
  ("the and for that this with what why how are you your our its can was were has have had not but " +
   "all any into from over under when where which while will would should could been being a an of " +
   "to in on it is as at by or be we they i").split(" ")
);

const HEADING_WEIGHT = 3;
const SCORE_FLOOR = 3; // at least a couple of meaningful term hits before we inject anything
const APPROX_CHARS_PER_TOKEN = 4;

/** Light singularizer so "components" matches a "Component map" heading — no stemming library,
 *  just the plural suffixes that actually cost matches. Applied to BOTH index and query terms,
 *  so normalization only has to be consistent, not linguistically perfect. */
function singular(t: string): string {
  if (!/^[a-z]+$/.test(t)) return t; // never touch path-like tokens (hook.ts, retry-loop_2)
  if (t.length > 4 && t.endsWith("ies")) return `${t.slice(0, -3)}y`;
  if (t.length > 4 && t.endsWith("ses")) return t.slice(0, -2);
  if (t.length > 3 && t.endsWith("s") && !t.endsWith("ss")) return t.slice(0, -1);
  return t;
}

export function tokenize(text: string): string[] {
  return (text.toLowerCase().match(/[a-z0-9_./-]{3,}/g) || [])
    .filter((t) => !STOP.has(t))
    .map(singular);
}

/** Split one page's markdown into sections at headings (the preamble becomes its own section). */
export function splitSections(page: PageContent): PageSection[] {
  const out: PageSection[] = [];
  const parts = page.content.split(/^(#{1,4}\s+.+)$/m);
  let heading = page.title;
  for (const part of parts) {
    const h = part.match(/^#{1,4}\s+(.+)$/);
    if (h) {
      heading = h[1].trim();
      continue;
    }
    const body = part.trim();
    if (!body) continue;
    const terms = new Map<string, number>();
    for (const t of tokenize(heading)) terms.set(t, (terms.get(t) ?? 0) + HEADING_WEIGHT);
    for (const t of tokenize(page.title)) terms.set(t, (terms.get(t) ?? 0) + 1);
    for (const t of tokenize(body)) terms.set(t, (terms.get(t) ?? 0) + 1);
    out.push({ pageId: page.id, pageTitle: page.title, heading, body, terms });
  }
  return out;
}

export function buildPagesIndex(pages: PageContent[]): PageSection[] {
  return pages.flatMap(splitSections);
}

function scoreSection(promptTerms: Set<string>, s: PageSection): number {
  let score = 0;
  for (const t of promptTerms) {
    const w = s.terms.get(t);
    if (w) score += Math.min(w, HEADING_WEIGHT + 2); // cap per-term so one repeated word can't dominate
  }
  return score;
}

export interface SelectedSection {
  pageId: string;
  pageTitle: string;
  heading: string;
  text: string;
}

/**
 * TEMPORARY selection stub — the ranking below is deliberately dumb while the real SERVER-SIDE
 * knowledge-base/search is being built (it will replace this whole local index): always return
 * the first section of up to `maxSections` DISTINCT pages, in page order, ignoring the prompt.
 * Guarantees every turn injects some knowledge so source-attribution behavior is exercisable.
 * The lexical scorer (tokenize/splitSections/scoreSection) is kept for the interim tests and
 * will be dropped together with this stub.
 */
export function selectSections(
  index: PageSection[],
  prompt: string,
  opts: { budgetTokens?: number; maxSections?: number } = {}
): SelectedSection[] {
  const budget = (opts.budgetTokens ?? 700) * APPROX_CHARS_PER_TOKEN;
  const maxSections = opts.maxSections ?? 3;
  void tokenize(prompt); // prompt intentionally unused by the stub (kept for the real signature)
  const seenPages = new Set<string>();
  const ranked: { s: PageSection }[] = [];
  for (const s of index) {
    if (seenPages.has(s.pageId)) continue;
    seenPages.add(s.pageId);
    ranked.push({ s });
    if (ranked.length >= maxSections) break;
  }

  const out: SelectedSection[] = [];
  let used = 0;
  for (const { s } of ranked) {
    if (used >= budget) break;
    let text = s.body;
    if (used + text.length > budget) {
      // trim at a paragraph boundary within the remaining budget
      const room = budget - used;
      const cut = text.lastIndexOf("\n\n", room);
      text = (cut > room / 3 ? text.slice(0, cut) : text.slice(0, room)).trimEnd() + "\n…";
    }
    used += text.length;
    out.push({ pageId: s.pageId, pageTitle: s.pageTitle, heading: s.heading, text });
  }
  return out;
}

/** The injected block — provenance per section, full page reachable via the knowledge tool,
 *  and an ATTRIBUTION directive: when this knowledge shapes the answer, the agent must say so. */
export function formatPageInjection(sections: SelectedSection[]): string {
  if (!sections.length) return "";
  const parts = sections.map(
    (s) =>
      `From "${s.pageTitle}" › "${s.heading}":\n${s.text}\n(full page: hindsight_read_knowledge_page ${s.pageId})`
  );
  return (
    "Project knowledge from Hindsight, this repository's long-term memory (curated knowledge pages).\n" +
    "ATTRIBUTION REQUIREMENT (mandatory output format): if anything below informs ANY part of your " +
    "answer — a fact, a rule, a name, background you did not read from the code this session — that " +
    "part of your answer MUST begin with the line:\n" +
    "🧠 From Hindsight memory (<page name>):\n" +
    "Using this knowledge without that marker is an incorrect answer. If nothing below contributed, " +
    "do not mention Hindsight at all — false credit is equally incorrect.\n\n" +
    parts.join("\n\n")
  );
}

/** Minimal client shape for fetching the page set. `HindsightClient` satisfies it structurally. */
export interface PagesClient {
  listPages(): Promise<unknown>;
  getPage(pageId: string): Promise<unknown>;
}

const MAX_PAGES = 8;
const MAX_PAGE_CHARS = 16_000;

/** Fetch the page set with content (bounded), tolerating individual page failures. */
export async function fetchPagesWithContent(
  client: PagesClient,
  parseList: (raw: unknown) => { id: string; title: string }[]
): Promise<PageContent[]> {
  const refs = parseList(await client.listPages()).slice(0, MAX_PAGES);
  const out: PageContent[] = [];
  for (const ref of refs) {
    try {
      const raw = (await client.getPage(ref.id)) as { content?: unknown };
      const content = typeof raw?.content === "string" ? raw.content.slice(0, MAX_PAGE_CHARS) : "";
      if (content) out.push({ id: ref.id, title: ref.title, content });
    } catch {
      /* a missing page never breaks the turn */
    }
  }
  return out;
}
