/**
 * Harness-agnostic runtime for PERSISTENT-PLUGIN harnesses (opencode): the recall → inject →
 * write-back state machine, held in memory across a long-lived process. It is the plugin-side
 * sibling of core/hook.ts (which drives the fresh-process HOOK harnesses); both share the same core
 * primitives — `buildSystemInjection`/`formatPageInjection`, `buildKnowledgePreamble`/`buildRosterRefresh`, `buildKnowledgeTools`,
 * `buildSessionStartContext` — so opencode reaches full v2 parity with Claude Code / Codex.
 *
 * A harness adapter feeds it three normalized events and reads two values back:
 *   - seedIfCold(repoPath)          : plugin load -> cold-check auto-seed + compute the page preamble
 *   - onPrompt(sessionId, prompt)   : each user turn -> recall + build this turn's injection
 *   - getInjection(sessionId)       : the system-prompt text to inject this turn (or undefined)
 *   - toolSpecs()                   : the hindsight_* knowledge/recall tools to register natively
 *   - onTranscript(sessionId, turns): full transcript -> write back every N turns (on by default)
 * No opencode/claude specifics live here — only the memory logic.
 */
import type { Config } from "./config";
import { diag } from "./diag";
import { setLogLevel } from "./log";
import type { HindsightClient } from "./hindsight";
import type { PageRef } from "./knowledge-injection";
import { buildRosterRefresh, parsePageList } from "./knowledge-injection";
import { brandWord } from "./brand";
import { buildSystemInjection } from "./inject";
import { buildKnowledgeTools, type ToolSpec } from "./knowledge-tools";
import { retainLiveSession, type TransportTurn } from "./chat";
import { buildSessionStartContext } from "./session-start";

const HARNESS = "opencode";

export class RuntimeCore {
  private readonly injection = new Map<string, string>(); // sessionId -> this turn's injection block
  private readonly turnCount = new Map<string, number>(); // sessionId -> user-turn counter (cadence)
  private readonly sessionState = new Map<string, { startTs: string; retainedUsers: number }>();
  private lastInjection = ""; // most recent turn's injection block, keyed by nothing (see getInjection)
  private readonly reflectBySession = new Map<string, string>(); // sessionId -> cached reflect ("" = ran, nothing)
  private pagesCache: PageRef[] | undefined; // page roster (ids + titles), refreshed on cadence
  private preamble = ""; // SessionStart-equivalent knowledge preamble, computed once at seedIfCold

  constructor(
    private readonly client: HindsightClient,
    private readonly bankId: string,
    private readonly cfg: Config
  ) {
    setLogLevel(cfg.logLevel);
  }

  /** The hindsight_* knowledge + recall tools, bound to this bank, for the harness to register natively. */
  toolSpecs(): ToolSpec[] {
    return buildKnowledgeTools(this.client, this.bankId);
  }

  get writeBackEnabled(): boolean {
    return this.cfg.retainSessions;
  }

  /**
   * Plugin load (SessionStart-equivalent): on a cold repo, deterministically start the background
   * git-log seed + codebase survey, and compute the knowledge-page preamble (tool guide + roster)
   * that onPrompt injects on the session's first turn. Reuses the exact hook-harness logic
   * (`buildSessionStartContext`) so opencode seeds identically. Never throws.
   */
  async seedIfCold(repoPath: string | undefined): Promise<void> {
    // Anti-recursion: a headless survey session runs the agent (which loads this plugin) with
    // HINDSIGHT_DISABLE_HOOKS=1 — the tools stay registered (toolSpecs, so the survey can ingest),
    // but seeding/recall/write-back must no-op or the survey would re-seed itself (see core/survey.ts).
    if (process.env.HINDSIGHT_DISABLE_HOOKS) return;
    try {
      const out = await buildSessionStartContext({
        cwd: repoPath || process.cwd(),
        bankId: this.bankId,
        cfg: this.cfg,
        client: this.client,
        harness: HARNESS,
      });
      // The seed note is user-facing; opencode has no visible-system channel at load, so surface it
      // on stderr (shows in the plugin log / console) rather than dropping it.
      if (out.systemMessage) console.error(out.systemMessage);
      this.preamble = out.additionalContext ?? "";
    } catch {
      /* seeding + preamble are best-effort — a cold-check failure never breaks the agent */
    }
  }

  /**
   * Each user turn: build this turn's injection block (reflect-once + page sections). Mirrors core/hook.ts's
   * `buildHookOutput`, but state lives in memory (persistent plugin) instead of a tmp cache file:
   *   - turn 1: prepend the knowledge preamble (the SessionStart-equivalent tool guide + page roster)
   *   - every turn: the recalled `<hindsight_memories>` block (attribution + user-feedback framing)
   *   - every pageRefreshEveryTurns: append the page-roster refresh so the tool guide keeps re-appearing
   */
  async onPrompt(sessionId: string | undefined, prompt: string): Promise<void> {
    if (process.env.HINDSIGHT_DISABLE_HOOKS) return; // anti-recursion (see seedIfCold)
    if (!sessionId || !prompt.trim()) return;
    const turns = (this.turnCount.get(sessionId) ?? 0) + 1;
    this.turnCount.set(sessionId, turns);

    // ── reflect: once per session, on its first prompt ──────────────────────────
    let reflectAnswer = this.reflectBySession.get(sessionId);
    let reflectRanThisTurn = false;
    if (reflectAnswer === undefined) {
      reflectRanThisTurn = true;
      const t0 = Date.now();
      try {
        reflectAnswer = await this.client.reflect(prompt, {
          budget: "high",
          timeoutMs: this.cfg.reflectTimeoutMs,
        });
        diag(HARNESS, reflectAnswer ? "reflect_ok" : "reflect_empty", {
          ms: Date.now() - t0,
          chars: reflectAnswer.length,
          query: prompt.slice(0, 80),
        });
      } catch (e) {
        reflectAnswer = ""; // ran and failed — don't retry every turn; the diag trail records it
        diag(HARNESS, "reflect_failed", {
          ms: Date.now() - t0,
          error: String((e as Error)?.message || e).slice(0, 200),
          query: prompt.slice(0, 80),
        });
      }
      this.reflectBySession.set(sessionId, reflectAnswer);
    }

    // ── knowledge pages: refetch on cadence, match locally every turn ───────────
    const refreshDue =
      this.cfg.pageRefreshEveryTurns > 0 && turns % this.cfg.pageRefreshEveryTurns === 0;
    if (refreshDue || this.pagesCache === undefined) {
      const t0 = Date.now();
      try {
        this.pagesCache = parsePageList(await this.client.listPages());
        diag(HARNESS, "pages_ok", { ms: Date.now() - t0, count: this.pagesCache.length });
      } catch (e) {
        this.pagesCache = this.pagesCache ?? [];
        diag(HARNESS, "pages_failed", {
          ms: Date.now() - t0,
          error: String((e as Error)?.message || e).slice(0, 200),
        });
      }
    }

    const blocks: string[] = [];
    // The preamble is the SessionStart-equivalent; inject it once, on the first turn (later turns get
    // the periodic refresh below). Empty until seedIfCold resolves — if the first prompt races ahead
    // of plugin-load seeding, the roster refresh still delivers the tool guide on cadence.
    if (turns === 1 && this.preamble) blocks.push(this.preamble);
    if (reflectAnswer) blocks.push(buildSystemInjection(reflectAnswer));
    // Knowledge pages are NOT auto-injected (phantom-research problem) — the agent pulls them
    // via the hindsight_search_knowledge_pages tool. Reflect-turn visibility only:
    if (reflectRanThisTurn && reflectAnswer) {
      const q = prompt.replace(/\s+/g, " ").trim();
      const excerpt = q.length > 48 ? `${q.slice(0, 48)}…` : q;
      const preview = reflectAnswer.replace(/\s+/g, " ").trim().slice(0, 140);
      console.error(
        `${brandWord()} · goal: recall this repo's past decisions about “${excerpt}”\n↳ ${preview}…`
      );
    }
    if (refreshDue) {
      blocks.push(buildRosterRefresh(this.pagesCache));
    }
    const block = blocks.filter(Boolean).join("\n\n");
    this.injection.set(sessionId, block);
    this.lastInjection = block; // for consumers that can't supply a sessionId (see getInjection)
  }

  /**
   * The system-prompt text to inject this turn (built by the preceding onPrompt), or undefined.
   * opencode's `experimental.chat.system.transform` hook fires with NO sessionId (input is just
   * `{model}`), so a session-keyed lookup returns nothing there — fall back to the most recent
   * turn's block (`lastInjection`). The completion's system.transform fires right after that
   * session's `chat.message`/onPrompt, so `lastInjection` is this turn's block.
   */
  getInjection(sessionId: string | undefined): string | undefined {
    const keyed = sessionId ? this.injection.get(sessionId) : undefined;
    return keyed ?? this.lastInjection ?? undefined;
  }

  /**
   * Full normalized transcript (rich: user/assistant text + tool calls/outputs): upsert every N user
   * turns when enabled. On by default for opencode (parity with the hook harnesses' Stop write-back),
   * so opencode sessions compound into memory; a user can opt out with `retainSessions: false`.
   */
  async onTranscript(sessionId: string, turns: TransportTurn[]): Promise<void> {
    if (process.env.HINDSIGHT_DISABLE_HOOKS) return; // anti-recursion (see seedIfCold)
    if (!this.writeBackEnabled || !sessionId || !turns.length) return;
    const users = turns.filter((t) => t.role === "user").length;
    let st = this.sessionState.get(sessionId);
    if (!st) {
      st = { startTs: new Date().toISOString(), retainedUsers: 0 };
      this.sessionState.set(sessionId, st);
    }
    if (users - st.retainedUsers >= this.cfg.retainEveryTurns) {
      st.retainedUsers = users;
      const t0 = Date.now();
      void retainLiveSession(this.client, sessionId, turns, st.startTs)
        .then(() =>
          diag(HARNESS, "retain_ok", { ms: Date.now() - t0, turns: turns.length, session: sessionId })
        )
        .catch((e) =>
          diag(HARNESS, "retain_failed", {
            ms: Date.now() - t0,
            error: String((e as Error)?.message || e).slice(0, 200),
            session: sessionId,
          })
        );
    }
  }

}
