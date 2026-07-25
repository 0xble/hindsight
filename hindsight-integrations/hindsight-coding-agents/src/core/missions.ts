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

export const CHAT_MISSION =
  "You are ingesting a raw developer conversation (a JSON user/assistant transcript). Extract the " +
  "FEWEST facts that capture the OUTCOME — do NOT emit one fact per message, per intermediate " +
  "proposal, or per tool step; that fragments the decision and reads as contradictory out of order. " +
  "Prefer: (1) ONE consolidated fact stating the FINAL, settled decision and its exact rule/values " +
  "unambiguously; and (2) at most one fact for the key alternative that was REJECTED and why. " +
  "CRITICAL: a conversation usually REVISES its answer — an early proposal gets changed. Record ONLY " +
  "the FINAL state as the decision / what is in effect. A superseded proposal must appear ONLY inside " +
  "the rejected fact ('initially proposed X, changed to Y because…'), NEVER as its own 'decided' " +
  "fact. If the same setting changes several times, keep only the LAST. Make unmistakably clear which " +
  "choice WON. Quote literal values/identifiers verbatim. Preserve the 'REF-ID: <token>' marker in " +
  "each fact. Do not invent; capture only what was actually settled.";

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

export const SESSION_MISSION =
  "You are ingesting a developer's LIVE coding-agent session: a markdown transcript of the user's " +
  "requests, the assistant's narration, and the concrete ACTIONS it took (file edits, commands run, " +
  "and their results, marked with '**Tool**' and '↳'). Unlike a short decision chat, a work session " +
  "usually contains SEVERAL durable facts — capture them. Extract: the DECISIONS made and their " +
  "rationale; the concrete CHANGES to specific code entities (files, functions, identifiers — quote " +
  "them VERBATIM); problems encountered and how they were resolved; and any conventions, invariants, " +
  "or constraints established. Consolidate related steps into coherent facts (do NOT emit one fact " +
  "per message or per tool call), and when the session revised its approach, record only the FINAL, " +
  "settled state as what is in effect. Preserve the 'REF-ID: <token>' marker verbatim in every fact.";

export const OBSERVATIONS_MISSION =
  "Consolidate durable knowledge about THIS codebase — recurring patterns, conventions, module " +
  "responsibilities, and how components relate — from the ingested commits and conversations. " +
  "Favor stable structural understanding over one-off details.";

// CUSTOM extraction prompt for chats — replaces the default extractor's rules entirely, so we get a
// TINY number of coherent facts (final decision + optional rejection), not a fact per message.
export const CHAT_CUSTOM_INSTRUCTIONS =
  "You are reading ONE developer conversation (JSON user/assistant turns) about a coding decision. It " +
  "typically PROPOSES options and then REVISES them — only the LAST state is real.\n\n" +
  "Extract AT MOST 2 facts:\n" +
  "1. THE DECISION — a single fact stating the FINAL, in-effect rule and its EXACT values/identifiers, " +
  'unambiguously (e.g. "the client pins API version v3 via the X-Api-Version header, not the URL path, ' +
  'as settled after the gateway migration"). Quote literals verbatim.\n' +
  '2. THE REJECTION (only if a notable alternative was tried) — one fact of the form "initially ' +
  'proposed X, but changed to Y because Z".\n\n' +
  "HARD RULES:\n" +
  "- NEVER emit a separate fact per message, per intermediate proposal, or per tool step.\n" +
  "- A superseded proposal appears ONLY inside fact #2 — NEVER as its own 'decided' fact.\n" +
  "- If a setting changed several times, keep ONLY the last as the decision.\n" +
  "- Emit just 1 fact when there is no meaningful rejected alternative.\n" +
  "- Preserve the 'REF-ID: <token>' marker from the transcript in each fact. Do not invent.";

export const RETAIN_STRATEGIES = {
  git: { retain_mission: GIT_MISSION, retain_extraction_mode: "verbose" },
  // ONE big aggregated document (last N commit messages, no diffs) -> a larger chunk size so it stays
  // in as few chunks as possible and the extractor sees the whole history arc at once.
  gitlog: {
    retain_mission: GITLOG_MISSION,
    retain_extraction_mode: "verbose",
    retain_chunk_size: 12000,
  },
  // chunk big enough to hold a WHOLE typical chat in ONE chunk (these run ~2.5k tokens / ~10k chars;
  // the 3000 default was SPLITTING them -> per-chunk fragments). ~12k stays well under a 16k-context
  // model, so the custom "≤2 facts" prompt sees the full proposal→revision arc and emits the final
  // decision. (Very long chats would still split and fall back to the consolidation layer.)
  chat: {
    retain_extraction_mode: "custom",
    retain_custom_instructions: CHAT_CUSTOM_INSTRUCTIONS,
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
  // LIVE coding-agent session write-back (core/retain-hook.ts, runtime.ts). A full work session is
  // NOT a short decision convo — the `chat` strategy's aggressive "≤2 facts" custom extraction would
  // throw away most of what the session produced. Verbose extraction over a big chunk captures the
  // several durable decisions/changes a real session makes. Distinct from `chat` (backfilled past
  // conversations, which ARE short decision logs and keep the ≤2-fact custom extractor).
  session: {
    retain_mission: SESSION_MISSION,
    retain_extraction_mode: "verbose",
    retain_chunk_size: 12000,
  },
} as const;

// ── passive tier tagging (entity_labels) ───────────────────────────────────────
// A single hierarchical bank-config group set by `configureBank` at seed time. `tag: true` makes the
// extractor copy each selected `knowledge:<value>` onto the fact's tags (via `_inject_label_tags`),
// so the seeded tier PAGES can scope their synthesis by tag with no extra query infra. The vocabulary
// is FIXED (not per-feature) because tag matching is exact set-ops with no wildcards.
export interface EntityLabelValue {
  value: string;
  description: string;
}

export interface EntityLabelGroup {
  key: string;
  type: "multi-values";
  optional: boolean;
  tag: boolean;
  description: string;
  values: EntityLabelValue[];
}

export const KNOWLEDGE_LABELS: EntityLabelGroup = {
  key: "knowledge",
  type: "multi-values", // 0, 1, or several — empty is normal
  optional: true,
  tag: true, // emits knowledge:<value> onto the fact's tags
  description:
    "Routing labels for this project's Hindsight KNOWLEDGE PAGES — curated, human-readable summaries " +
    "of the repo's DURABLE engineering knowledge (architecture, key decisions, conventions, ongoing " +
    "initiatives), each page rebuilt automatically from the facts labeled for it. Mark a fact only when " +
    "it is durable, reusable knowledge a developer would still want surfaced in future sessions. " +
    "IMPORTANT: leave this EMPTY for routine, transient, or operational facts — a passing test, a " +
    "one-off command, a status update, a debugging dead-end. MOST facts should get no label here. " +
    "Assign more than one value only when the fact genuinely fits several.",
  values: [
    {
      value: "feature-work",
      description:
        "A new feature, initiative, or enhancement being planned or built — the capability being added " +
        "and the intent behind it. Not routine bug-fixes or chores.",
    },
    {
      value: "decision",
      description:
        "A technical decision that will constrain future work, with its rationale — why this approach " +
        "was chosen over alternatives, or a rule deliberately adopted.",
    },
    {
      value: "convention",
      description:
        "An established way this project does things — naming, structure, testing, error handling, or " +
        "another recurring pattern a contributor is expected to follow.",
    },
    {
      value: "component",
      description:
        "What a specific module, file, service, or subsystem is responsible for, or how components " +
        "depend on and connect to one another.",
    },
    {
      value: "concept",
      description:
        "A domain concept, key abstraction, or piece of project vocabulary a new contributor must " +
        "understand to work effectively.",
    },
  ],
};

// Knowledge PAGES (OKF pages = mental models) = a developer's durable mental model of the codebase,
// CONSOLIDATED from the ingested MEMORY (commit history + past conversations) — NOT mirrored from the
// current source (which would need constant re-sync). A universal 5-page taxonomy that generalizes to
// any repo; the curator populates each from history+chats and can spawn per-component sub-pages.
// A seeded knowledge page = a saved, tag-scoped synthesis view. `tags` pins the page to one
// `knowledge:<tier>` label so its synthesis draws ONLY from facts the extractor tagged for that tier
// (exact set-ops, no wildcards — see KNOWLEDGE_LABELS). Names/tiers mirror the entity-label vocabulary.
export interface KnowledgePage {
  name: string;
  source_query: string;
  tags: string[];
}

export const PAGES: KnowledgePage[] = [
  {
    name: "Component map",
    source_query:
      "From this project's commit history and past discussions, what are the main " +
      "components/modules/subsystems, what is each responsible for, and how do they relate to or " +
      "depend on one another? Describe the structure and responsibilities.",
    tags: ["knowledge:component"],
  },
  {
    name: "Core concepts",
    source_query:
      "What are the core concepts, domain abstractions, and key entities in this project — " +
      "the vocabulary a developer must understand? For each, explain what it represents and its role, " +
      "drawn from how they are introduced and discussed across the history and conversations.",
    tags: ["knowledge:concept"],
  },
  {
    name: "Conventions and patterns",
    source_query:
      "What conventions, idioms, and recurring patterns does this project follow — its " +
      "approach to testing, error handling, naming, structure, and how changes are typically made? " +
      "Describe how THIS project does things, as evidenced across its history and discussions.",
    tags: ["knowledge:convention"],
  },
  {
    name: "Key decisions and rationale",
    source_query:
      "What are the significant technical decisions made in this project and the rationale " +
      "behind them — the durable 'why we do it this way' a developer should know? Summarize the " +
      "decisions and their reasoning from the commit rationales and past conversations.",
    tags: ["knowledge:decision"],
  },
  {
    name: "Initiatives and enhancements",
    source_query:
      "Based on this repository's commit history, what are the major initiatives, features, and " +
      "enhancements the project has worked on? Summarize the themes and notable changes over time. " +
      "When a source memory carries a tag of the form `relatedPageId:<id>`, include a Markdown link " +
      "`[[page:<id>]]` to that page in the summary, so each initiative links to its detailed page.",
    tags: ["knowledge:feature-work"],
  },
];
