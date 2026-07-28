/**
 * Shared runtime for HOOK-based harnesses (Claude Code, Codex, Cursor CLI, ...).
 *
 * The ONE runtime path (see docs/superpowers/specs/2026-07-27-reflect-pages-runtime.md):
 *   - REFLECT once per session, on the first prompt: agentic synthesis over the bank returning the
 *     root-cause decision with exact values. Cached per session, re-injected every turn.
 *   - KNOWLEDGE PAGES every turn: the page set is fetched on a cadence and matched LOCALLY against
 *     the prompt (section-level lexical scoring — no server/LLM call); the top sections are
 *     injected with provenance. Fast like recall, organized like reflect.
 *   - The tool-guide/page-roster block re-injects on the same cadence.
 *
 * Every outcome is recorded in the diagnostics file — a memory-less session can't masquerade as a
 * memory session. A failure never breaks the agent.
 *
 * A harness plugs in with a HookSpec: its name, how to read (prompt, cwd, sessionId) from its
 * stdin event, and how to wrap injected context in its native output schema. The pure logic lives
 * in `buildHookOutput` (client + cache file in, injection string out) so it's unit-testable
 * without stdin/stdout; `runHook` is thin plumbing around it, with a `makeClient` seam for tests.
 */
import { existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { deriveBankId } from "./bank";
import type { Config } from "./config";
import { loadConfig } from "./config";
import { diag } from "./diag";
import { log, setLogLevel } from "./log";
import { startBackgroundSeed } from "./seed";
import { startCodebaseSurvey, type SurveyHarness } from "./survey";
import type { ClientOpts } from "./hindsight";
import { HindsightClient } from "./hindsight";
import { brandWord } from "./brand";
import { buildSystemInjection } from "./inject";
import type { PageRef } from "./knowledge-injection";
import { buildRosterRefresh, parsePageList } from "./knowledge-injection";

export interface HookEventFields {
  prompt?: string;
  cwd?: string;
  sessionId?: string;
}

export interface HookSpec {
  /** Harness name — config `harnesses.<name>` section, {harness} template field, diag records. */
  harness: string;
  /** Harnesses with NO SessionStart-equivalent hook (Cursor): fire the ingestion engine from the
   *  FIRST prompt of each session instead, so auto-ingestion parity doesn't depend on a hook the
   *  host doesn't offer. */
  ensureSeed?: boolean;
  /** Read the fields out of the harness's stdin event (shapes differ per harness). */
  parse(event: Record<string, unknown>): HookEventFields;
  /** Wrap injected context (and an optional user-facing notice) in the harness's native
   *  hook-output schema. Harnesses whose schema has no user-visible channel ignore `notice`. */
  emit(context: string, notice?: string): unknown;
}

/** Minimal client shape `buildHookOutput` needs — `HindsightClient` satisfies it structurally. */
interface HookClient {
  reflect(query: string, opts: { budget?: string; timeoutMs?: number }): Promise<string>;
  listPages(): Promise<unknown>;
  /** Only needed by ensureSeed's cold check (optional so test doubles stay minimal). */
  listDocumentIds?(tag: string): Promise<Set<string>>;
}

/** Hook harnesses run under the host's per-hook kill window; never let reflect outlive it. */
const HOOK_REFLECT_CAP_MS = 25_000;

interface SessionCache {
  turns?: number;
  reflectAnswer?: string; // present (even "") = reflect already ran this session
  pages?: { atTurn: number; list: PageRef[] };
}



export interface HookOutput {
  /** The model-facing injection block, or undefined when there's nothing to inject. */
  context?: string;
  /** User-facing line(s) — set only on the reflect turn (its goal + result preview). */
  notice?: string;
}

/**
 * Pure hook logic: reflect once per session (cached); knowledge-page sections + roster on every
 * turn. Returns the injection plus a per-turn user-facing notice.
 */
export async function buildHookOutput(args: {
  harness: string;
  prompt: string;
  cfg: Config;
  client: HookClient;
  cacheFile: string;
}): Promise<HookOutput> {
  const { harness, prompt, cfg, client, cacheFile } = args;

  let cached: SessionCache = {};
  try {
    cached = JSON.parse(readFileSync(cacheFile, "utf8")) as SessionCache;
  } catch {
    /* missing/invalid cache — first prompt of the session */
  }
  const turns = (cached.turns ?? 0) + 1;

  // ── reflect: once per session, on the first prompt ────────────────────────────
  let reflectAnswer = cached.reflectAnswer;
  let reflectRanThisTurn = false;
  if (reflectAnswer === undefined) {
    reflectRanThisTurn = true;
    const t0 = Date.now();
    try {
      reflectAnswer = await client.reflect(prompt, {
        budget: "high",
        timeoutMs: Math.min(cfg.reflectTimeoutMs, HOOK_REFLECT_CAP_MS),
      });
      diag(harness, reflectAnswer ? "reflect_ok" : "reflect_empty", {
        ms: Date.now() - t0,
        chars: reflectAnswer.length,
        query: prompt.slice(0, 80),
      });
    } catch (e) {
      reflectAnswer = ""; // ran and failed — don't retry every turn; the diag trail records it
      log.warn(harness, "reflect failed — session runs without memory", {
        error: String((e as Error)?.message || e).slice(0, 200),
      });
      diag(harness, "reflect_failed", {
        ms: Date.now() - t0,
        error: String((e as Error)?.message || e).slice(0, 200),
        query: prompt.slice(0, 80),
      });
    }
  }

  // ── knowledge-page roster (ids + titles only): refreshed on the cadence ────────
  const cadence = cfg.pageRefreshEveryTurns;
  const stale =
    !cached.pages || (cadence > 0 && turns - cached.pages.atTurn >= cadence);
  let pages = cached.pages?.list ?? [];
  if (stale) {
    const t0 = Date.now();
    try {
      pages = parsePageList(await client.listPages());
      diag(harness, "pages_ok", { ms: Date.now() - t0, count: pages.length });
    } catch (e) {
      diag(harness, "pages_failed", {
        ms: Date.now() - t0,
        error: String((e as Error)?.message || e).slice(0, 200),
      });
    }
  }

  // Persist the session cache (best-effort).
  try {
    mkdirSync(dirname(cacheFile), { recursive: true });
    writeFileSync(
      cacheFile,
      JSON.stringify({
        turns,
        reflectAnswer,
        pages: { atTurn: stale ? turns : (cached.pages?.atTurn ?? turns), list: pages },
      } satisfies SessionCache)
    );
  } catch {
    /* cache is best-effort */
  }

  const blocks: string[] = [];
  if (reflectAnswer) blocks.push(buildSystemInjection(reflectAnswer));
  // Knowledge pages are NOT auto-injected: the agent pulls them through
  // hindsight_search_knowledge_pages when a question warrants it — an unprompted injection on
  // every turn (even a plain "yes") read as phantom research. The roster below keeps the tool
  // and the page names in front of the agent.
  if (cadence > 0 && turns % cadence === 0) {
    blocks.push(buildRosterRefresh(pages));
  }
  const kept = blocks.filter(Boolean);

  // User-facing notice ONLY on the turn reflect actually ran (showing its assigned goal and a
  // preview of what came back). Ordinary turns stay silent — page knowledge is now pulled via
  // the hindsight_search_knowledge_pages tool, which is visible as a real tool call.
  let notice: string | undefined;
  if (reflectRanThisTurn && reflectAnswer) {
    const q = prompt.replace(/\s+/g, " ").trim();
    const excerpt = q.length > 48 ? `${q.slice(0, 48)}…` : q;
    const preview = reflectAnswer.replace(/\s+/g, " ").trim();
    notice =
      `${brandWord()} · goal: recall this repo's past decisions about “${excerpt}”\n` +
      `↳ ${preview.length > 140 ? `${preview.slice(0, 140)}…` : preview}`;
  }

  return { context: kept.length ? kept.join("\n\n") : undefined, notice };
}

/** Run one hook invocation: stdin event in, (maybe) an injection object on stdout. */
export async function runHook(
  spec: HookSpec,
  makeClient: (opts: ClientOpts) => HookClient = (o) => new HindsightClient(o)
): Promise<void> {
  // Anti-recursion: the codebase survey's own headless session sets this so its hooks are a no-op.
  if (process.env.HINDSIGHT_DISABLE_HOOKS) return;

  let ev: Record<string, unknown> = {};
  try {
    ev = JSON.parse(readFileSync(0, "utf8")) as Record<string, unknown>;
  } catch {
    return; // no/invalid event: stay silent
  }
  const { prompt: rawPrompt, cwd: rawCwd, sessionId } = spec.parse(ev);
  const cwd = rawCwd || process.cwd();
  const prompt = (rawPrompt || "").trim();
  if (!prompt) return;

  const cfg = loadConfig({ harness: spec.harness });
  setLogLevel(cfg.logLevel);
  if (cfg.disabled) {
    log.debug(spec.harness, "hook skipped: disabled");
    return;
  }

  const out = (context: string | undefined, notice?: string) =>
    process.stdout.write(JSON.stringify(spec.emit(context ?? "", notice)));

  const client = makeClient({
    apiUrl: cfg.apiUrl,
    apiToken: cfg.apiToken,
    bank: deriveBankId(cfg, cwd, spec.harness),
  });
  const cacheFile = join(tmpdir(), `hindsight-${spec.harness}`, `${sessionId || "no-session"}.json`);

  // SessionStart-parity for hosts without a session-start hook: on the session's FIRST prompt
  // (no cache file yet), fire the idempotent ingestion engine, and the cold-only survey.
  if (spec.ensureSeed && cfg.autoSeed !== false && !existsSync(cacheFile)) {
    startBackgroundSeed(cwd, { limit: cfg.seedLimit });
    if (cfg.codebaseSurvey !== false && client.listDocumentIds) {
      void client
        .listDocumentIds("source:git")
        .then((ids) => {
          if (ids.size === 0) {
            diag(spec.harness, "seed_started", { bank: deriveBankId(cfg, cwd, spec.harness) });
            startCodebaseSurvey(cwd, {
              harness: spec.harness as SurveyHarness,
              model: cfg.surveyModel,
              budgetUsd: cfg.surveyBudgetUsd,
            });
          }
        })
        .catch(() => {});
    }
  }

  const output = await buildHookOutput({ harness: spec.harness, prompt, cfg, client, cacheFile });
  if (output.context || output.notice) out(output.context, output.notice);
}
