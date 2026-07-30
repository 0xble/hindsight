/**
 * Host adapter runtime for PERSISTENT-PLUGIN harnesses (opencode). It delegates SessionStart and
 * prompt behavior to the same core lifecycle as fresh-process hook harnesses; this class keeps only
 * the host-specific injection, toast, and incremental-transcript responsibilities.
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
import { log, setLogLevel } from "./log";
import type { HindsightClient } from "./hindsight";
import { buildKnowledgeTools, type ToolSpec } from "./knowledge-tools";
import { retainLiveSession, type TransportTurn } from "./chat";
import { buildSessionStartContext } from "./session-start";
import { buildHookOutput } from "./hook";
import { sessionCacheFile, writeSessionCache } from "./session-cache";

const HARNESS = "opencode";

export class RuntimeCore {
  private readonly injection = new Map<string, string>(); // sessionId -> this turn's injection block
  private readonly turnCount = new Map<string, number>(); // sessionId -> user-turn counter (cadence)
  private readonly sessionState = new Map<string, { startTs: string; retainedUsers: number }>();
  private lastInjection = ""; // most recent turn's injection block, keyed by nothing (see getInjection)
  private deferInitialReflect = false;
  /** Host-notice channel (opencode: client.tui.showToast via the adapter). Optional, fail-open. */
  private notify?: (title: string, message: string) => void;
  private preamble = ""; // SessionStart-equivalent knowledge preamble, computed once at seedIfCold

  constructor(
    private readonly client: HindsightClient,
    private readonly bankId: string,
    private readonly cfg: Config
  ) {
    setLogLevel(cfg.logLevel);
  }

  setNotifier(notify: (title: string, message: string) => void): void {
    this.notify = notify;
  }

  /** The hindsight_* knowledge + recall tools, bound to this bank, for the harness to register natively. */
  toolSpecs(): ToolSpec[] {
    return buildKnowledgeTools(this.client, this.bankId, { harness: HARNESS });
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
      // Banner: via the host's toast API when available (stderr corrupts the TUI); always logged.
      if (out.systemMessage) {
        const plain = out.systemMessage.replace(/\x1b\[[0-9;]*m/g, "");
        log.info(HARNESS, plain);
        this.notify?.("Hindsight", plain.replace(/^Hindsight is /, "Is "));
      }
      this.preamble = out.additionalContext ?? "";
      this.deferInitialReflect = out.deferInitialReflect === true;
    } catch {
      /* seeding + preamble are best-effort — a cold-check failure never breaks the agent */
    }
  }

  /**
   * Each user turn delegates to the same `buildHookOutput` used by hook harnesses. The cache file
   * intentionally replaces the old in-memory reflect/page state, keeping lifecycle behavior
   * identical across hosts while this adapter retains only delivery-specific state.
   */
  async onPrompt(sessionId: string | undefined, prompt: string): Promise<void> {
    if (process.env.HINDSIGHT_DISABLE_HOOKS) return; // anti-recursion (see seedIfCold)
    if (!sessionId || !prompt.trim()) return;
    const turns = (this.turnCount.get(sessionId) ?? 0) + 1;
    this.turnCount.set(sessionId, turns);

    const cacheFile = sessionCacheFile(HARNESS, sessionId);
    if (this.deferInitialReflect) {
      // `seedIfCold` has no session id. Transfer its SessionStart decision to the first concrete
      // session here; `buildHookOutput` consumes it exactly once, like hook harnesses do.
      this.deferInitialReflect = false;
      writeSessionCache(cacheFile, { deferInitialReflect: true });
    }
    const output = await buildHookOutput({
      harness: HARNESS,
      prompt,
      cfg: this.cfg,
      client: this.client,
      cacheFile,
    });

    const blocks: string[] = [];
    // The preamble is the SessionStart-equivalent; inject it once, on the first turn (later turns get
    // the periodic refresh below). Empty until seedIfCold resolves — if the first prompt races ahead
    // of plugin-load seeding, the roster refresh still delivers the tool guide on cadence.
    if (turns === 1 && this.preamble) blocks.push(this.preamble);
    if (output.context) blocks.push(output.context);
    // OpenCode has no user-message hook channel; use its native toast instead of stderr, which
    // renders inside the TUI input line. The shared output owns when a notice exists.
    if (output.notice) {
      const preview = output.notice.replace(/\s+/g, " ").trim().slice(0, 140);
      log.info(HARNESS, "reflect goal", { preview });
      this.notify?.("Hindsight · recalled past decisions", preview);
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
      void retainLiveSession(this.client, sessionId, turns, st.startTs, HARNESS)
        .then(() =>
          diag(HARNESS, "retain_ok", {
            ms: Date.now() - t0,
            turns: turns.length,
            session: sessionId,
          })
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
