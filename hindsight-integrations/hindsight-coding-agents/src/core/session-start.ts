/**
 * Claude Code `SessionStart` hook: deterministically auto-seeds a cold repo's bank from its git
 * history (in the background, non-blocking) and injects a short visible note plus the live
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
import { startCodebaseSurvey } from "./survey";
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
  stateDir?: string;
  hasGit?: (dir: string) => boolean;
  startSeed?: (repoDir: string, opts?: { limit?: number }) => void;
  startSurvey?: (repoDir: string, opts?: { model?: string; budgetUsd?: number }) => void;
}): Promise<SessionStartOutput> {
  const { cwd, bankId, cfg, client, stateDir } = args;
  const hasGit = args.hasGit ?? hasGitHistory;
  const startSeed = args.startSeed ?? startBackgroundSeed;
  const startSurvey = args.startSurvey ?? startCodebaseSurvey;

  // The cold-seed note is USER-FACING and must ride `systemMessage` (the only hook field Claude
  // Code renders in the terminal); `additionalContext` is model-only and would show the human
  // nothing. The knowledge preamble is model context and stays in `additionalContext`.
  let systemMessage: string | undefined;

  if (cfg.autoSeed !== false) {
    if (hasGit(cwd)) {
      const state = readSeedState(bankId, stateDir);
      if (!state.declined && !state.seededAt) {
        let docIds: Set<string> | undefined;
        try {
          docIds = await client.listDocumentIds("source:git");
        } catch {
          docIds = undefined; // server unreachable: transient — don't write state, try again next session
        }

        if (docIds !== undefined) {
          if (docIds.size > 0) {
            // Warm (or already seeded by another path): remember, so we don't re-enumerate every session.
            writeSeedState(bankId, { seededAt: new Date().toISOString() }, stateDir);
          } else {
            // Cold: start the background seed now, deterministically — no agent involvement.
            startSeed(cwd, { limit: cfg.seedLimit });
            if (cfg.codebaseSurvey !== false) {
              startSurvey(cwd, { model: cfg.surveyModel, budgetUsd: cfg.surveyBudgetUsd });
            }
            writeSeedState(bankId, { seededAt: new Date().toISOString() }, stateDir);
            diag("claude-code", "seed_started", { bank: bankId });
            systemMessage =
              `🧠 Hindsight is learning ${bankId} from this repo's git history in the background, ` +
              `and surveying the codebase structure to build knowledge pages — recalled memories will ` +
              `appear as it processes. No action needed.`;
          }
        }
      }
    }
  }

  // Inject the live knowledge-page roster + guidance preamble. Fail-open: a listPages rejection
  // yields an empty roster (empty-state preamble) and never disturbs the seed logic above.
  const pages = parsePageList(await client.listPages().catch(() => null));
  const additionalContext = buildKnowledgePreamble(pages);

  return { systemMessage, additionalContext };
}

/** Run one SessionStart hook invocation: stdin event in, (maybe) an additionalContext object on stdout. */
export async function runSessionStartHook(
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

    const cfg = loadConfig({ harness: "claude-code", projectDir: cwd });
    if (cfg.disabled) return;

    const bankId = deriveBankId(cfg, cwd, "claude-code");
    const client = makeClient({ apiUrl: cfg.apiUrl, apiToken: cfg.apiToken, bank: bankId });

    const out = await buildSessionStartContext({ cwd, bankId, cfg, client });
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
