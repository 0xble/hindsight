/**
 * Claude Code `SessionStart` hook: deterministically auto-seeds a cold repo's bank from its git
 * history (in the background, non-blocking), keeps warm banks deepening on every start, and
 * injects a short visible note plus the live
 * knowledge-page roster + guidance preamble that tells the agent to consult the repo's pages.
 *
 * Earlier design injected an instruction asking the AGENT to pose a y/n question to the user and
 * then run a seed command itself. Live testing showed that doesn't work: the model surfaces the
 * question, then plows ahead with the user's actual task and never runs the command — so nothing
 * ever seeds. Fix: the hook does the seeding itself. There is nothing left for the agent to decide
 * or execute for seeding, so no shell command (and no shell-escaping) is needed anymore.
 *
 * Tri-state cold check: a boolean "is it cold?" would collapse "cold" and "server unreachable"
 * into the same outcome, which is wrong here — a transient outage must not get treated the same
 * as "already seeded" and silently suppress seeding forever. So `buildSessionStartContext` calls
 * `client.listDocumentIds` directly so it can tell the three cases apart:
 *   - throws (server unreachable)      -> no seed, no state written  (try again next session)
 *   - non-empty set (warm/pre-seeded)  -> no seed, seededAt written  (remember, skip enumerating)
 *   - empty set (cold)                 -> start the background seed, seededAt written, note added
 */
import { readFileSync } from "node:fs";
import { hasGitHistory } from "./git";
import { readSeedState, writeSeedState, startBackgroundSeed } from "./seed";
import { startCodebaseSurvey, type SurveyHarness } from "./survey";
import { loadConfig } from "./config";
import type { Config } from "./config";
import { deriveBankId } from "./bank";
import { diag } from "./diag";
import { parsePageList, buildKnowledgePreamble } from "./knowledge-injection";
import type { ClientOpts } from "./hindsight";
import { HindsightClient } from "./hindsight";

/** Minimal client shape `buildSessionStartContext` needs. */
interface SeedContextClient {
  listDocumentIds(tag: string): Promise<Set<string>>;
  listPages(): Promise<unknown>;
}

/**
 * The Hindsight LOGO — byte-identical to the API server's startup banner (hindsight_api/banner.py):
 * half-block pixel art with 24-bit ANSI gradient colors, so the plugin and the server share one
 * visual identity. Requires a truecolor ANSI terminal; if a host ever renders it as escape soup,
 * fall back to plain text here.
 */
const LOGO =
  "  \x1b[38;2;9;127;184m\u2584\x1b[0m\x1b[48;2;8;130;178m\x1b[38;2;5;133;186m\u2584\x1b[0m       \x1b[48;2;10;143;160m\x1b[38;2;10;143;165m\u2584\x1b[0m\x1b[38;2;7;140;156m\u2584\x1b[0m\n \x1b[38;2;8;125;192m\u2584\x1b[0m \x1b[38;2;3;132;191m\u2580\x1b[0m\x1b[38;2;2;133;192m\u2584\x1b[0m \x1b[38;2;3;132;180m\u2584\x1b[0m\x1b[38;2;1;137;184m\u2584\x1b[0m\x1b[38;2;3;133;174m\u2584\x1b[0m \x1b[38;2;3;142;176m\u2584\x1b[0m\x1b[38;2;4;142;169m\u2580\x1b[0m \x1b[38;2;10;144;164m\u2584\x1b[0m\n\x1b[38;2;6;121;195m\u2580\x1b[0m\x1b[38;2;5;128;203m\u2580\x1b[0m\x1b[48;2;5;124;195m\x1b[38;2;3;125;200m\u2584\x1b[0m\x1b[38;2;2;126;196m\u2584\x1b[0m\x1b[48;2;3;128;188m\x1b[38;2;1;131;196m\u2584\x1b[0m\x1b[48;2;0;152;219m\x1b[38;2;2;131;191m\u2584\x1b[0m\x1b[38;2;1;141;196m\u2580\x1b[0m\x1b[38;2;1;135;183m\u2580\x1b[0m\x1b[38;2;1;148;198m\u2580\x1b[0m\x1b[48;2;1;156;202m\x1b[38;2;2;135;180m\u2584\x1b[0m\x1b[48;2;4;134;169m\x1b[38;2;1;137;177m\u2584\x1b[0m\x1b[38;2;3;138;173m\u2584\x1b[0m\x1b[48;2;6;137;165m\x1b[38;2;2;140;170m\u2584\x1b[0m\x1b[38;2;7;144;169m\u2580\x1b[0m\x1b[38;2;7;139;158m\u2580\x1b[0m\n   \x1b[48;2;2;128;202m\x1b[38;2;2;124;201m\u2584\x1b[0m\x1b[48;2;1;130;201m\x1b[38;2;0;135;212m\u2584\x1b[0m\x1b[38;2;2;128;196m\u2584\x1b[0m \x1b[48;2;2;142;204m\x1b[38;2;7;138;199m\u2584\x1b[0m \x1b[38;2;1;135;186m\u2584\x1b[0m\x1b[48;2;1;142;186m\x1b[38;2;2;144;194m\u2584\x1b[0m\x1b[48;2;3;138;176m\x1b[38;2;2;134;176m\u2584\x1b[0m\n \x1b[48;2;8;118;200m\x1b[38;2;8;121;209m\u2584\x1b[0m\x1b[38;2;3;121;203m\u2580\x1b[0m \x1b[38;2;3;122;192m\u2580\x1b[0m\x1b[38;2;1;138;216m\u2580\x1b[0m\x1b[48;2;0;138;210m\x1b[38;2;3;128;198m\u2584\x1b[0m\x1b[48;2;0;126;188m\x1b[38;2;2;131;198m\u2584\x1b[0m\x1b[48;2;0;142;205m\x1b[38;2;3;132;193m\u2584\x1b[0m\x1b[38;2;1;140;196m\u2580\x1b[0m  \x1b[38;2;4;134;175m\u2580\x1b[0m\x1b[48;2;13;135;167m\x1b[38;2;8;136;174m\u2584\x1b[0m ";

/**
 * The session banner: the server's logo over one plain line naming the repo's bank. Shown on
 * EVERY session start — Hindsight's presence should be visible, not inferred. The line reads
 * "learning" on a cold repo (first ingest running) and "remembering" once the bank is warm.
 */
export function buildSeedBanner(bankId: string, cold = true): string {
  return `${LOGO}\n\n  Hindsight is ${cold ? "learning" : "remembering"} this repo → memory bank “${bankId}”`;
}

/** Split SessionStart output: `systemMessage` renders in the terminal (user-visible);
 *  `additionalContext` is injected into the model's context only. */
export interface SessionStartOutput {
  systemMessage?: string;
  additionalContext?: string;
}

/**
 * Build this session's additionalContext: (maybe) kick off a background auto-seed of a cold repo
 * and (always, unless the hook is disabled) append the knowledge-page bank mission. See module doc
 * for the tri-state cold check. Never throws.
 */
export async function buildSessionStartContext(args: {
  cwd: string;
  bankId: string;
  cfg: Config;
  client: SeedContextClient;
  harness?: string;
  stateDir?: string;
  hasGit?: (dir: string) => boolean;
  startSeed?: (repoDir: string, opts?: { limit?: number }) => void;
  startSurvey?: (
    repoDir: string,
    opts?: { harness?: SurveyHarness; model?: string; budgetUsd?: number }
  ) => void;
}): Promise<SessionStartOutput> {
  const { cwd, bankId, cfg, client, stateDir } = args;
  const t0 = Date.now();
  let cold: boolean | undefined; // undefined = not checked (autoSeed off / non-git / declined / unreachable)
  const harness = args.harness ?? "claude-code";
  const hasGit = args.hasGit ?? hasGitHistory;
  const startSeed = args.startSeed ?? startBackgroundSeed;
  const startSurvey = args.startSurvey ?? startCodebaseSurvey;

  // The banner is USER-FACING and must ride `systemMessage` (the only hook field Claude Code
  // renders in the terminal); `additionalContext` is model-only and would show the human nothing.
  // The knowledge preamble is model context and stays in `additionalContext`.
  let systemMessage: string | undefined;

  if (cfg.autoSeed !== false) {
    if (hasGit(cwd)) {
      const state = readSeedState(bankId, stateDir);
      // Only `declined` hard-opts-out. We DON'T gate on a stored `seededAt`: the LIVE bank is the
      // source of truth (cold-check-wins). A stale `seededAt` from an earlier seed must not suppress
      // re-seeding after the user has cleared the bank — otherwise "delete the bank + restart" never
      // re-seeds. Cost: one `listDocumentIds` per session start (cheap; usually a single page).
      if (!state.declined) {
        let docIds: Set<string> | undefined;
        try {
          docIds = await client.listDocumentIds("source:git");
        } catch {
          docIds = undefined; // server unreachable: transient — do nothing, try again next session
        }

        cold = docIds !== undefined ? docIds.size === 0 : undefined;
        if (docIds !== undefined) {
          // ALWAYS fire the background deepen engine when the server is reachable — it is
          // idempotent (per-bank lock, dedup by document id) and each run does only the missing
          // work: cold seed, newly appeared conversations, the next per-commit diff batch. The
          // one-time extras stay cold-gated below.
          startSeed(cwd, { limit: cfg.seedLimit });
          // Cold iff the bank has zero source:git docs (an undefined result — server error — is
          // NOT treated as cold; we never surveyed/noted on an unconfirmed-empty bank).
          if (docIds.size === 0) {
            if (cfg.codebaseSurvey !== false) {
              // Run the survey under the current harness's own CLI (falls back to any available agent).
              startSurvey(cwd, {
                harness: harness as SurveyHarness,
                model: cfg.surveyModel,
                budgetUsd: cfg.surveyBudgetUsd,
              });
            }
            // Record the seed time (informational — no longer a gate).
            writeSeedState(bankId, { seededAt: new Date().toISOString() }, stateDir);
            diag(harness, "seed_started", { bank: bankId });
          }
        }
      }
    }
  }

  // Inject the live knowledge-page roster + guidance preamble. Fail-open: a listPages rejection
  // yields an empty roster (empty-state preamble) and never disturbs the seed logic above.
  const pages = parsePageList(await client.listPages().catch(() => null));
  const additionalContext = buildKnowledgePreamble(pages);

  // The banner shows on EVERY session — Hindsight's presence is part of the product, not a
  // one-time setup note. Wording tracks the bank state: cold = "learning", else "remembering".
  systemMessage = buildSeedBanner(bankId, cold === true);

  // ALWAYS record the session start (warm sessions used to log nothing — undebuggable).
  diag(harness, "session_start", { bank: bankId, cold, pages: pages.length, ms: Date.now() - t0 });

  return { systemMessage, additionalContext };
}

/** Run one SessionStart hook invocation: stdin event in, (maybe) an additionalContext object on stdout. */
export async function runSessionStartHook(
  harness = "claude-code",
  makeClient: (opts: ClientOpts) => SeedContextClient = (o) => new HindsightClient(o)
): Promise<void> {
  // Anti-recursion: the codebase survey's own headless claude session (core/survey.ts) sets this
  // so its hooks are a no-op — it must not re-seed/re-survey its own survey session.
  if (process.env.HINDSIGHT_DISABLE_HOOKS) return;

  // Whole-body try/catch (unlike runHook/runRetainHook, which only guard individual steps): a
  // throw here happens during session bootstrap, before the agent has done anything — more
  // disruptive than a failure mid-prompt — so nothing in this function may ever escape it.
  try {
    let ev: Record<string, unknown> = {};
    try {
      ev = JSON.parse(readFileSync(0, "utf8")) as Record<string, unknown>;
    } catch {
      return; // no/invalid event: stay silent
    }
    const cwd = (ev.cwd as string) || process.cwd();

    const cfg = loadConfig({ harness, projectDir: cwd });
    if (cfg.disabled) return;

    const bankId = deriveBankId(cfg, cwd, harness);
    const client = makeClient({ apiUrl: cfg.apiUrl, apiToken: cfg.apiToken, bank: bankId });

    const out = await buildSessionStartContext({ cwd, bankId, cfg, client, harness });
    // `systemMessage` is top-level (Claude Code renders it to the USER); `additionalContext`
    // nests under hookSpecificOutput (model context only).
    const payload: {
      systemMessage?: string;
      hookSpecificOutput: { hookEventName: string; additionalContext?: string };
    } = { hookSpecificOutput: { hookEventName: "SessionStart" } };
    if (out.systemMessage) payload.systemMessage = out.systemMessage;
    if (out.additionalContext) payload.hookSpecificOutput.additionalContext = out.additionalContext;
    if (out.systemMessage || out.additionalContext) {
      process.stdout.write(JSON.stringify(payload));
    }
  } catch {
    /* SessionStart must never throw and break the session */
  }
}
