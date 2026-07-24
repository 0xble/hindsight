/**
 * Claude Code `SessionStart` hook: offers to auto-seed a cold repo's bank from its git history.
 *
 * Tri-state cold check (deliberately NOT `isColdRepo` from core/seed.ts, which collapses "cold"
 * and "server unreachable" into one false — fine for a fail-open feature-gate, wrong here where a
 * transient outage must not get treated the same as "already seeded" and silently suppress the
 * offer forever). `buildSessionOffer` calls `client.listDocumentIds` directly so it can tell the
 * three cases apart:
 *   - throws (server unreachable)      -> no offer, no state written  (ask again next session)
 *   - non-empty set (warm/pre-seeded)  -> no offer, seededAt written  (remember, skip enumerating)
 *   - empty set (cold)                 -> return the offer string
 */
import { readFileSync } from "node:fs";
import { hasGitHistory } from "./git";
import { readSeedState, writeSeedState } from "./seed";
import { loadConfig } from "./config";
import { deriveBankId } from "./bank";
import { diag } from "./diag";
import type { ClientOpts } from "./hindsight";
import { HindsightClient } from "./hindsight";

/** Minimal client shape `buildSessionOffer` needs. */
interface SeedOfferClient {
  listDocumentIds(tag: string): Promise<Set<string>>;
}

function offerText(cwd: string, pluginRoot: string): string {
  const seedCmd = `node "${pluginRoot}/dist/hindsight-seed.js" seed --repo "${cwd}"`;
  const declineCmd = `node "${pluginRoot}/dist/hindsight-seed.js" decline --repo "${cwd}"`;
  return `🧠 Hindsight has no memory of this repository yet.

Before continuing, ask the user one yes/no question:
"Want me to have Hindsight learn this repo from its git history so I can recall past decisions? It runs in the background and won't interrupt your work. (Repo: ${cwd})"

- If YES, run exactly this once (it starts a background seed and returns immediately):
  ${seedCmd}
  then tell the user Hindsight is learning in the background, and continue with their task.
- If NO or "not now", run:
  ${declineCmd}
  so you are not asked again for this repo.

Ask this only once, at the very start of the session.`;
}

/**
 * Pure-ish offer builder: given a bank/client/cwd, decide whether to offer to seed and return the
 * additionalContext string (or undefined). See module doc for the tri-state cold check.
 */
export async function buildSessionOffer(args: {
  cwd: string;
  bankId: string;
  pluginRoot: string;
  client: SeedOfferClient;
  stateDir?: string;
  hasGit?: (dir: string) => boolean;
}): Promise<string | undefined> {
  const { cwd, bankId, pluginRoot, client, stateDir } = args;

  if (!(args.hasGit ?? hasGitHistory)(cwd)) return undefined;

  const state = readSeedState(bankId, stateDir);
  if (state.declined || state.seededAt) return undefined;

  let docIds: Set<string>;
  try {
    docIds = await client.listDocumentIds("source:git");
  } catch {
    return undefined; // server unreachable: transient — don't write state, ask again next session
  }

  if (docIds.size > 0) {
    // Warm (or already seeded by another path): remember, so we don't re-enumerate every session.
    writeSeedState(bankId, { seededAt: new Date().toISOString() }, stateDir);
    return undefined;
  }

  return offerText(cwd, pluginRoot);
}

/** Run one SessionStart hook invocation: stdin event in, (maybe) an additionalContext object on stdout. */
export async function runSessionStartHook(
  makeClient: (opts: ClientOpts) => SeedOfferClient = (o) => new HindsightClient(o)
): Promise<void> {
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

    const pluginRoot = process.env.CLAUDE_PLUGIN_ROOT;
    if (!pluginRoot) return; // can't form the commands without it

    const bankId = deriveBankId(cfg, cwd, "claude-code");
    const client = makeClient({ apiUrl: cfg.apiUrl, apiToken: cfg.apiToken, bank: bankId });

    const offer = await buildSessionOffer({ cwd, bankId, pluginRoot, client });
    if (offer) {
      process.stdout.write(
        JSON.stringify({
          hookSpecificOutput: { hookEventName: "SessionStart", additionalContext: offer },
        })
      );
      diag("claude-code", "seed_offer", { bank: bankId });
    }
  } catch {
    /* SessionStart must never throw and break the session */
  }
}
