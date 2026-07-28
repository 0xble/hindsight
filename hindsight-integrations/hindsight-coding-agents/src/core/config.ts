/**
 * ONE config file: ~/.hindsight/coding-agent.json (JSON, no environment variables — the sole
 * exception is HINDSIGHT_DIAG_FILE for the diagnostics path).
 *
 * Layering, later wins per field:
 *   1. built-in defaults
 *   2. the config file's top level
 *   3. its `harnesses.<name>` section for the asking harness
 *
 * There is deliberately NO project-local config: a second, repo-carried file was both a security
 * surface (untrusted repos influencing memory behavior) and a second place to look. Per-repo bank
 * routing is `directoryBankMap`; per-agent differences are `harnesses.<name>`.
 */
import { readFileSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";
import { DEFAULT_SEED_LIMIT } from "./seed";

/** Default config-file path: ~/.hindsight/coding-agent.json */
export const CONFIG_PATH = join(homedir(), ".hindsight", "coding-agent.json");

/** Incremental git-sync settings (see core/sync.ts). */
/** The config file's shape — every field optional; omitted fields take the documented default. */
export interface RawConfig {
  apiUrl?: string; // Hindsight API base URL (default https://api.hindsight.vectorize.io — Cloud; set to http://localhost:8888 for a local server)
  apiToken?: string; // bearer token (optional)
  bankId?: string; // EXPLICIT memory bank id — set = static bank; unset = per-repo dynamic (core/bank.ts)
  dynamicBankId?: boolean; // force dynamic resolution even when bankId is set (default: dynamic iff no bankId)
  bankIdTemplate?: string; // dynamic bank id format (default "coding-agent::{gitProject}" — harness-neutral) —
  //   placeholders: {gitProject} {project} {harness} {channel} {user} (see core/bank.ts)
  directoryBankMap?: Record<string, string>; // absolute path -> bank; longest prefix wins; overrides everything
  resolveWorktrees?: boolean; // {gitProject}: worktrees share the main repo's bank (default true)
  harness?: string; // runtime adapter (default "opencode")
  disabled?: boolean; // hard off-switch — inert plugin, for a no-memory baseline (default false)
  retainSessions?: boolean; // opencode plugin write-back (default true; set false to opt out). Hook harnesses always write back on Stop and ignore this flag.
  retainEveryTurns?: number; // write-back cadence in user turns (default 1: async upsert every turn)
  reflectTimeoutMs?: number; // session-start reflect timeout (default 120000; hooks cap lower internally)
  pageRefreshEveryTurns?: number; // knowledge-page refresh cadence in user turns (default 10)
  autoSeed?: boolean; // SessionStart: auto-seed a cold repo's bank from git history (default true)
  seedLimit?: number; // SessionStart auto-seed: most-recent-N-commits cap (default 300)
  codebaseSurvey?: boolean; // SessionStart: spawn a headless claude to survey a cold repo's structure (default true)
  surveyModel?: string; // model passed to the headless survey's `claude -p --model` (default "haiku")
  surveyBudgetUsd?: number; // spend cap passed to the headless survey's `claude -p --max-budget-usd` (default 2)
  /** How git history feeds memory — seeding AND keeping current use the same engine:
   *  "message" = commit messages only (cheap aggregated doc, re-upserted when HEAD moves);
   *  "full"    = messages + every recent commit's full diff (progressive batches, newest first);
   *  "none"    = git ingestion off entirely. Default "message" (cheap by default; opt into depth). */
  gitIngest?: "message" | "full" | "none";
  /** Per-harness overrides of any of the fields above, keyed by harness name ("opencode",
   *  "claude-code", ...). Lets one config file give each agent its own bank/settings. */
  harnesses?: Record<string, Omit<RawConfig, "harnesses">>;
}

/** Fully-resolved config: every field present. */
export interface Config {
  apiUrl: string;
  apiToken?: string;
  bankId?: string; // resolved per-directory via deriveBankId(cfg, dir) — see core/bank.ts
  dynamicBankId?: boolean;
  bankIdTemplate?: string;
  directoryBankMap?: Record<string, string>;
  resolveWorktrees?: boolean;
  harness: string;
  disabled: boolean;
  retainSessions: boolean;
  retainEveryTurns: number;
  reflectTimeoutMs: number;
  pageRefreshEveryTurns: number;
  autoSeed: boolean;
  seedLimit: number;
  codebaseSurvey: boolean;
  surveyModel: string;
  surveyBudgetUsd: number;
  gitIngest: "message" | "full" | "none";
}

/** Apply defaults to a raw (file) config. Pure — the single place the defaults live. */
export function resolveConfig(raw: RawConfig = {}): Config {
  return {
    apiUrl: raw.apiUrl ?? "https://api.hindsight.vectorize.io",
    apiToken: raw.apiToken || undefined,
    bankId: raw.bankId,
    dynamicBankId: raw.dynamicBankId,
    bankIdTemplate: raw.bankIdTemplate,
    directoryBankMap: raw.directoryBankMap,
    resolveWorktrees: raw.resolveWorktrees,
    harness: raw.harness ?? "opencode",
    disabled: raw.disabled ?? false,
    retainSessions: raw.retainSessions ?? true, // opencode: write back by default (parity with hook-harness Stop)
    retainEveryTurns: raw.retainEveryTurns || 1,
    reflectTimeoutMs: raw.reflectTimeoutMs || 120000,
    pageRefreshEveryTurns: raw.pageRefreshEveryTurns || 10,
    autoSeed: raw.autoSeed ?? true,
    seedLimit: raw.seedLimit || DEFAULT_SEED_LIMIT,
    codebaseSurvey: raw.codebaseSurvey ?? true,
    surveyModel: raw.surveyModel || "haiku",
    surveyBudgetUsd: raw.surveyBudgetUsd || 2,
    gitIngest: ["message", "full", "none"].includes(raw.gitIngest as string)
      ? (raw.gitIngest as "message" | "full" | "none")
      : "message",
  };
}

function readRaw(path: string): RawConfig {
  try {
    return JSON.parse(readFileSync(path, "utf8")) as RawConfig;
  } catch (e) {
    if ((e as NodeJS.ErrnoException)?.code !== "ENOENT") {
      console.error(`hindsight: ignoring invalid config at ${path}: ${(e as Error)?.message || e}`);
    }
    return {};
  }
}

/** Shallow-merge b over a; `harnesses` never survives into a layer. */
function mergeRaw(a: RawConfig, b: RawConfig): RawConfig {
  const { harnesses: _drop, ...flat } = b;
  return { ...a, ...flat };
}

/**
 * Keys a PROJECT-LOCAL (untrusted, repo-supplied) config may NOT set. These control where the user's
 * credential + prompts/transcripts are sent (`apiUrl`, `apiToken`) or remap banks globally
 * (`directoryBankMap`, which "overrides everything"). A repo can still set its own per-repo bank via
 * `bankId`/`bankIdTemplate` — just not the network endpoint, token, or global path→bank map. The
 * user-global config is trusted and unrestricted.
 */
export interface LoadOptions {
  /** Which harness is asking ("opencode", "claude-code", ...) — applies its `harnesses.<name>` overrides. */
  harness?: string;
  /** Explicit global-config path (default ~/.hindsight/coding-agent.json). */
  path?: string;
}

/** Apply one config layer (its top level, then its `harnesses.<name>` override) over `raw`. */
function applyLayer(raw: RawConfig, layer: RawConfig, harness?: string): RawConfig {
  let out = mergeRaw(raw, layer);
  const perHarness = harness ? layer.harnesses?.[harness] : undefined;
  if (perHarness) out = mergeRaw(out, perHarness);
  return out;
}

/** Load + resolve config from THE config file. Missing file -> silent defaults. */
export function loadConfig(opts: LoadOptions | string = {}): Config {
  const o: LoadOptions = typeof opts === "string" ? { path: opts } : opts; // legacy: loadConfig(path)
  return resolveConfig(applyLayer({}, readRaw(o.path ?? CONFIG_PATH), o.harness));
}
