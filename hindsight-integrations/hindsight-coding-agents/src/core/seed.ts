/**
 * Cold-repo detection + per-bank seed-consent state, for the auto-seed flow (Task 10).
 *
 * Fail-safe throughout: isColdRepo only reports "cold" when it can positively confirm no
 * git-sourced docs exist (network errors -> false, never offer to seed on a guess). State
 * read/write never throws — a corrupt or unwritable state file must not break the hook.
 */
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";

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

/** Read persisted per-bank seed state. Missing file or invalid JSON -> {} (never throws). */
export function readSeedState(bankId: string, stateDir: string = DEFAULT_STATE_DIR): SeedState {
  try {
    return JSON.parse(readFileSync(statePath(bankId, stateDir), "utf8")) as SeedState;
  } catch {
    return {};
  }
}

/** Merge `patch` into the persisted per-bank seed state and write it. Best-effort: never throws
 *  (a failed write must not break a hook). */
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
