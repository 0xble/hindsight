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
import { gitHeadSha, hasGitHistory } from "./git";
import { DEEPEN_DIFF_TARGET } from "./status";
import { startBackgroundSeed } from "./seed";
import { startCodebaseSurvey, type SurveyHarness } from "./survey";
import { loadConfig } from "./config";
import type { Config } from "./config";
import { deriveBankId } from "./bank";
import { brandWord } from "./brand";
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
 * The session banner: one line — the gradient "Hindsight" wordmark (same colors as the API
 * server's banner) plus the repo's bank. Shown on EVERY session start; "learning" on a cold
 * repo (first ingest running), "remembering" once the bank is warm.
 */
export function buildSeedBanner(bankId: string, cold = true, gitNote?: string): string {
  // Two lines: what Hindsight DOES for this repo (value, not mechanism), then where the memory
  // lives + sync state. The brand word leads line 1 so the host TUI's message prefix lands there.
  const headline = cold
    ? `${brandWord()} is learning this repo — ingesting its decisions, conventions and history`
    : `${brandWord()} is tracking the decisions, conventions and history of this repo`;
  const details = `  ↳ memory bank “${bankId}”` + (gitNote ? ` · ${gitNote}` : "");
  return `${headline}\n${details}`;
}

/**
 * One-phrase git-sync state for the banner (the syncStatus contract, condensed): whether the bank
 * is current with the repo's commits. Cheap — reuses the cold-check's doc-id set plus ONE tag query
 * (gitlog-head:<sha>, the freshness marker the deepen engine maintains). Returns undefined when
 * there's nothing meaningful to say (gitIngest off, no git, cold bank — "learning" already covers it).
 */
async function gitSyncNote(args: {
  client: SeedContextClient;
  cwd: string;
  gitIds: Set<string>;
  mode: "message" | "full" | "none";
  cold: boolean;
}): Promise<string | undefined> {
  const { client, cwd, gitIds, mode, cold } = args;
  if (mode === "none" || cold) return undefined;
  const head = gitHeadSha(cwd);
  if (!head) return undefined;
  const gitlogCurrent = await client
    .listDocumentIds(`gitlog-head:${head}`)
    .then((s) => s.size > 0)
    .catch(() => undefined);
  if (gitlogCurrent === undefined) return undefined; // server hiccup: say nothing rather than guess
  if (mode === "message") return gitlogCurrent ? "git in sync" : "catching up on new commits";
  // full: deepening progress = per-commit docs vs the recent-history target
  const deepened = [...gitIds].filter((id) => id.startsWith("git:")).length;
  let target = DEEPEN_DIFF_TARGET;
  try {
    const { execFileSync } = await import("node:child_process");
    const n = Number(
      execFileSync("git", ["-C", cwd, "rev-list", "--count", "HEAD"], { encoding: "utf8" }).trim()
    );
    if (n > 0) target = Math.min(DEEPEN_DIFF_TARGET, n);
  } catch {
    /* keep the default target */
  }
  return gitlogCurrent && deepened >= target
    ? "git in sync"
    : `syncing git history (${Math.min(deepened, target)}/${target})`;
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
  let cold: boolean | undefined; // undefined = not checked (autoSeed off / non-git / unreachable)
  let gitDocIds: Set<string> | undefined; // the cold-check's doc set, reused by the banner's git note
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
      // The LIVE bank is the ONLY state (cold-check-wins): delete the bank and the next session is
      // a true first-open — no client-side flags can contradict it. Opting out of memory for a repo
      // is `disabled` in project config, not a hidden state file.
      {
        let docIds: Set<string> | undefined;
        try {
          docIds = await client.listDocumentIds("source:git");
        } catch {
          docIds = undefined; // server unreachable: transient — do nothing, try again next session
        }

        cold = docIds !== undefined ? docIds.size === 0 : undefined;
        gitDocIds = docIds;
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
  // one-time setup note. Wording tracks the bank state: cold = "learning", else "remembering";
  // warm banners also condense the syncStatus contract into a git-sync phrase.
  let gitNote: string | undefined;
  if (gitDocIds) {
    gitNote = await gitSyncNote({
      client,
      cwd,
      gitIds: gitDocIds,
      mode: cfg.gitIngest,
      cold: cold === true,
    }).catch(() => undefined);
  }
  systemMessage = buildSeedBanner(bankId, cold === true, gitNote);

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

    const cfg = loadConfig({ harness });
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
