/**
 * Harness-agnostic Hindsight missions, retain strategies, and knowledge-page taxonomy.
 *
 * These describe HOW a coding project's memory is extracted and reasoned over. They are independent
 * of which agent harness produced the sessions, so they live in the shared core and are reused by
 * every harness adapter.
 */

// ── retain missions (git vs chat need different extraction) ─────────────────────
export const GIT_MISSION =
  "You are ingesting a single git commit: its message and its full diff. Extract the concrete " +
  "technical DECISION and the CAUSE/INVARIANT it encodes, bound to the specific code entities " +
  "(functions, methods, files) and behaviors it changes. Preserve exact identifiers, paths, and " +
  "literal values verbatim. Preserve the 'REF-ID: <token>' marker verbatim in every fact. Capture " +
  "both WHAT changed and WHY.";

export const GITLOG_MISSION =
  "You are ingesting an aggregated block of git commit MESSAGES ONLY (no diffs) — the project's " +
  "recent commit-message history, newest first. Extract the project's INITIATIVES, FEATURES, " +
  "ENHANCEMENTS, and notable changes or THEMES over time — what the project has been working on and " +
  "how it has evolved. Do NOT extract per-line code detail (there is no diff to draw it from). Group " +
  "related commits into a coherent initiative/theme where the messages make that clear; preserve exact " +
  "identifiers and literal values verbatim when quoting a subject line.";

export const CONVERSATION_MISSION =
  "You are ingesting a developer conversation as a JSON transcript of turns ({role, content}): the " +
  "user's requests, the assistant's narration, and compact 'action' turns naming each tool use and " +
  "its target (e.g. \"Edit boltons/strutils.py\") with no arguments or outputs. It may be a SHORT " +
  "decision chat or a LONG working session — scale the facts to the substance, never to the message " +
  "count. Extract the FEWEST facts that capture the OUTCOME: the settled DECISIONS and their exact " +
  "rules/values (quote literals VERBATIM); concrete CHANGES to specific code entities; problems and " +
  "how they were resolved; conventions or invariants established; at most one fact for a notable " +
  "REJECTED alternative ('initially proposed X, changed to Y because Z'). A short decision chat " +
  "usually yields 1-2 facts; a substantial working session several. CRITICAL: a conversation REVISES " +
  "itself — record ONLY the FINAL state as what is in effect; a superseded proposal appears ONLY " +
  "inside the rejected fact, NEVER as its own 'decided' fact; if the same setting changes several " +
  "times keep only the LAST, and make unmistakably clear which choice WON. Do NOT emit one fact per " +
  "message, per intermediate proposal, or per action turn. Preserve the 'REF-ID: <token>' marker " +
  "verbatim in every fact. Do not invent; capture only what was actually settled.";

export const REFLECT_MISSION =
  "You are a debugging assistant with the project's past decisions in memory (git rationale and " +
  "developer chats). Given a bug's SYMPTOM, find the past decision whose rationale explains the ROOT " +
  "CAUSE — not one that merely shares vocabulary. Answer with the PRECISE fix: state the EXACT rule " +
  "and the LITERAL values, identifiers, strings, numbers, or set members that were decided — quote " +
  "them VERBATIM, never paraphrase, generalize, or omit them (give the actual decided value, not " +
  "'the project standard'). If memories CONFLICT on the same rule, the LATEST decision wins — " +
  "prefer facts that explicitly amend or supersede an earlier one, state the superseded rule as " +
  "no longer in effect, and never present it as the fix. Name the function/file to change and " +
  "cite the REF-ID(s).";

export const DOCUMENT_MISSION =
  "You are ingesting a standalone document (notes, docs, or structural findings). Extract the " +
  "concrete facts, concepts, and structure it describes.";

export const OBSERVATIONS_MISSION =
  "Consolidate durable knowledge about THIS codebase — recurring patterns, conventions, module " +
  "responsibilities, and how components relate — from the ingested commits and conversations. " +
  "Favor stable structural understanding over one-off details.";

export const RETAIN_STRATEGIES = {
  git: { retain_mission: GIT_MISSION, retain_extraction_mode: "verbose" },
  // ONE big aggregated document (last N commit messages, no diffs) -> a larger chunk size so it stays
  // in as few chunks as possible and the extractor sees the whole history arc at once.
  gitlog: {
    retain_mission: GITLOG_MISSION,
    retain_extraction_mode: "verbose",
    retain_chunk_size: 12000,
  },
  // ONE strategy for ALL developer conversations — backfilled decision chats and live working
  // sessions alike (they are the same content type in the same JSON transcript format; the mission
  // scales extraction to the substance, final-state-wins). Chunk big enough to hold a whole typical
  // conversation in ONE chunk so the extractor sees the full proposal→revision arc (the 3000
  // default SPLIT them into per-chunk fragments); very long sessions still split and fall back to
  // the consolidation layer.
  conversation: {
    retain_mission: CONVERSATION_MISSION,
    retain_extraction_mode: "verbose",
    retain_chunk_size: 12000,
  },
  // Structural documents (e.g. the codebase survey's ingested findings) aren't dialogue — the
  // chat strategy's "final decision vs rejected proposal" extraction doesn't apply. Verbose mode
  // with a bigger chunk size (documents can run long) captures the concrete facts/structure instead.
  document: {
    retain_mission: DOCUMENT_MISSION,
    retain_extraction_mode: "verbose",
    retain_chunk_size: 12000,
  },
} as const;

// Knowledge PAGES (OKF pages = mental models) = a developer's durable mental model of the codebase,
// CONSOLIDATED from the ingested MEMORY (commit history + past conversations) — NOT mirrored from the
// current source (which would need constant re-sync). A universal 5-page taxonomy that generalizes to
// any repo; the curator populates each from history+chats and can spawn per-component sub-pages.
// Each page is UNSCOPED: its `source_query` selects the relevant facts from the whole bank at
// synthesis time — no tag taxonomy for the extractor to maintain.
export interface KnowledgePage {
  name: string;
  source_query: string;
}

export const PAGES: KnowledgePage[] = [
  {
    name: "Component map",
    source_query:
      "From this project's commit history and past discussions, what are the main " +
      "components/modules/subsystems, what is each responsible for, and how do they relate to or " +
      "depend on one another? Describe the structure and responsibilities.",
  },
  {
    name: "Core concepts",
    source_query:
      "What are the core concepts, domain abstractions, and key entities in this project — " +
      "the vocabulary a developer must understand? For each, explain what it represents and its role, " +
      "drawn from how they are introduced and discussed across the history and conversations.",
  },
  {
    name: "Conventions and patterns",
    source_query:
      "What conventions, idioms, and recurring patterns does this project follow — its " +
      "approach to testing, error handling, naming, structure, and how changes are typically made? " +
      "Describe how THIS project does things, as evidenced across its history and discussions.",
  },
  {
    name: "Key decisions and rationale",
    source_query:
      "What are the significant technical decisions made in this project and the rationale " +
      "behind them — the durable 'why we do it this way' a developer should know? Summarize the " +
      "decisions and their reasoning from the commit rationales and past conversations.",
  },
  {
    name: "Initiatives and enhancements",
    source_query:
      "Based on this repository's commit history, what are the major initiatives, features, and " +
      "enhancements the project has worked on? Summarize the themes and notable changes over time. " +
      "When a source memory carries a tag of the form `relatedPageId:<id>`, include a Markdown link " +
      "`[[page:<id>]]` to that page in the summary, so each initiative links to its detailed page.",
  },
];
