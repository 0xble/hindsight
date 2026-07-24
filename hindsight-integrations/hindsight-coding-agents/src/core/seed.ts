/**
 * Cold-repo detection + per-bank seed-consent state, for the auto-seed flow (Task 10).
 *
 * Fail-safe throughout: isColdRepo only reports "cold" when it can positively confirm no
 * git-sourced docs exist (network errors -> false, never offer to seed on a guess). State
 * read/write never throws — a corrupt or unwritable state file must not break the hook.
 */
import { spawn as realSpawn } from "node:child_process";
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { homedir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

export interface SeedState {
  declined?: boolean; // user said no to seeding this bank — don't re-ask every session
  seededAt?: string; // ISO timestamp of the last successful seed, if any
}

const DEFAULT_STATE_DIR = join(homedir(), ".hindsight", "coding-agent-state");

/** True if the bank has no git-sourced documents (never backfilled). Fail-SAFE: any error
 *  (e.g. server unreachable) returns false — we only offer to seed when we can positively
 *  confirm the bank is cold. */
export async function isColdRepo(client: {
  listDocumentIds(tag: string): Promise<Set<string>>;
}): Promise<boolean> {
  try {
    return (await client.listDocumentIds("source:git")).size === 0;
  } catch {
    return false;
  }
}

function statePath(bankId: string, stateDir: string): string {
  return join(stateDir, encodeURIComponent(bankId) + ".json");
}

/** Read persisted per-bank seed state. Missing file, invalid JSON, or valid-but-wrong-shaped
 *  JSON (e.g. a hand-edited file containing a bare string/number/array) -> {} (never throws). */
export function readSeedState(bankId: string, stateDir: string = DEFAULT_STATE_DIR): SeedState {
  try {
    const v: unknown = JSON.parse(readFileSync(statePath(bankId, stateDir), "utf8"));
    return v && typeof v === "object" && !Array.isArray(v) ? (v as SeedState) : {};
  } catch {
    return {};
  }
}

/** Merge `patch` into the persisted per-bank seed state and write it. Best-effort: never throws
 *  (a failed write must not break a hook). Not atomic: the read-merge-write is not locked, so a
 *  concurrent write for the same bank could lose a patch — accepted trade-off for low-frequency
 *  per-bank consent state. */
export function writeSeedState(
  bankId: string,
  patch: SeedState,
  stateDir: string = DEFAULT_STATE_DIR
): void {
  try {
    const merged = { ...readSeedState(bankId, stateDir), ...patch };
    mkdirSync(stateDir, { recursive: true });
    writeFileSync(statePath(bankId, stateDir), JSON.stringify(merged));
  } catch {
    /* best-effort: never throw */
  }
}

export const DEFAULT_SEED_LIMIT = 300;

/** Spawn a DETACHED background backfill of the most-recent `limit` commits of `repoDir`. Fire-and-forget:
 *  the child outlives this process; extraction is server-side/async so nothing here blocks. Never throws. */
export function startBackgroundSeed(
  repoDir: string,
  opts: { limit?: number; backfillPath?: string; spawn?: typeof realSpawn } = {}
): void {
  try {
    const spawnFn = opts.spawn ?? realSpawn;
    const backfillPath =
      opts.backfillPath ?? join(dirname(fileURLToPath(import.meta.url)), "backfill.js");
    const limit = opts.limit ?? DEFAULT_SEED_LIMIT;
    const child = spawnFn("node", [backfillPath, "--repo", repoDir, "--limit", String(limit)], {
      detached: true,
      stdio: "ignore",
    });
    child.unref();
  } catch {
    /* best-effort: a failed spawn must not break the caller */
  }
}

export interface SeedControlResult {
  ok: boolean;
  message: string;
}

/** Execute a seed-control command. Pure-ish: bank + connection are passed in; spawn + stateDir injectable. */
export function seedControl(
  command: string,
  args: {
    repo: string;
    bankId: string;
    limit?: number;
    spawn?: typeof realSpawn;
    stateDir?: string;
  }
): SeedControlResult {
  if (command === "seed") {
    startBackgroundSeed(args.repo, { limit: args.limit, spawn: args.spawn });
    writeSeedState(args.bankId, { seededAt: new Date().toISOString() }, args.stateDir);
    return {
      ok: true,
      message: `Hindsight is learning ${args.bankId} from its git history in the background — memories will appear as it processes.`,
    };
  }
  if (command === "decline") {
    writeSeedState(args.bankId, { declined: true }, args.stateDir);
    return { ok: true, message: `Okay — won't offer to seed ${args.bankId} again.` };
  }
  return { ok: false, message: "usage: hindsight-seed <seed|decline> --repo <dir>" };
}
