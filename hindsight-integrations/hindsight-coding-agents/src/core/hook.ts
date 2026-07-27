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
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { deriveBankId } from "./bank";
import type { Config } from "./config";
import { loadConfig } from "./config";
import { diag } from "./diag";
import type { ClientOpts } from "./hindsight";
import { HindsightClient } from "./hindsight";
import { brandWord } from "./brand";
import { buildSystemInjection } from "./inject";
import { buildRosterRefresh, parsePageList } from "./knowledge-injection";
import type { PageContent } from "./pages-index";
import { buildPagesIndex, fetchPagesWithContent, formatPageInjection, selectSections } from "./pages-index";

export interface HookEventFields {
  prompt?: string;
  cwd?: string;
  sessionId?: string;
}

export interface HookSpec {
  /** Harness name — config `harnesses.<name>` section, {harness} template field, diag records. */
  harness: string;
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
  getPage(pageId: string): Promise<unknown>;
}

/** Hook harnesses run under the host's per-hook kill window; never let reflect outlive it. */
const HOOK_REFLECT_CAP_MS = 25_000;

interface SessionCache {
  turns?: number;
  reflectAnswer?: string; // present (even "") = reflect already ran this session
  pages?: { atTurn: number; list: PageContent[] };
}



export interface HookOutput {
  /** The model-facing injection block, or undefined when there's nothing to inject. */
  context?: string;
  /** One user-facing line saying what Hindsight retrieved this turn (memory must be VISIBLE). */
  notice: string;
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
  if (reflectAnswer === undefined) {
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
      diag(harness, "reflect_failed", {
        ms: Date.now() - t0,
        error: String((e as Error)?.message || e).slice(0, 200),
        query: prompt.slice(0, 80),
      });
    }
  }

  // ── knowledge pages: fetched on the roster cadence, matched locally every turn ─
  const cadence = cfg.pageRefreshEveryTurns;
  const stale =
    !cached.pages || (cadence > 0 && turns - cached.pages.atTurn >= cadence);
  let pages = cached.pages?.list ?? [];
  if (stale) {
    const t0 = Date.now();
    try {
      pages = await fetchPagesWithContent(client, parsePageList);
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
  const sections = selectSections(buildPagesIndex(pages), prompt);
  if (sections.length) blocks.push(formatPageInjection(sections));
  if (cadence > 0 && turns % cadence === 0) {
    blocks.push(buildRosterRefresh(pages.map((p) => ({ id: p.id, title: p.title }))));
  }
  const kept = blocks.filter(Boolean);

  // The per-turn user-facing notice: the brand plus what went into context this turn.
  const q = prompt.replace(/\s+/g, " ").trim();
  const excerpt = q.length > 48 ? `${q.slice(0, 48)}…` : q;
  const pageTitles = [...new Set(sections.map((s) => s.pageTitle))];
  const notice = pageTitles.length
    ? `${brandWord()} · “${excerpt}” → ${pageTitles.join(", ")}`
    : `${brandWord()} · memory warming up`;

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

  const cfg = loadConfig({ harness: spec.harness, projectDir: cwd });
  if (cfg.disabled) return;

  const out = (context: string | undefined, notice: string) =>
    process.stdout.write(JSON.stringify(spec.emit(context ?? "", notice)));

  const client = makeClient({
    apiUrl: cfg.apiUrl,
    apiToken: cfg.apiToken,
    bank: deriveBankId(cfg, cwd, spec.harness),
  });
  const cacheFile = join(tmpdir(), `hindsight-${spec.harness}`, `${sessionId || "no-session"}.json`);

  const output = await buildHookOutput({ harness: spec.harness, prompt, cfg, client, cacheFile });
  if (output.context || output.notice) out(output.context, output.notice);
}
