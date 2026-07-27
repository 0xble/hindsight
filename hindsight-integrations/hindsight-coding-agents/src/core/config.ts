/**
 * Harness-agnostic configuration — JSON files, NO environment variables.
 *
 * All entry points read this: the runtime adapters (opencode plugin, claude hook) and the backfill
 * CLI (whose --flags override it). Files are optional: missing -> defaults; malformed -> one warning
 * + defaults (memory never breaks the agent).
 *
 * Layering (later wins, per field):
 *   1. built-in defaults
 *   2. ~/.hindsight/coding-agent.json            (user-global — TRUSTED, unrestricted)
 *   3.   its `harnesses.<name>` section          (per-agent override, e.g. different bankId for claude)
 *   4. <project>/.hindsight/coding-agent.json    (project-local — UNTRUSTED: comes from the repo; sanitized)
 *   5.   its `harnesses.<name>` section
 *
 * Each runtime entry point knows which harness it IS (the opencode plugin is loaded by opencode, the
 * claude hook by Claude Code) and passes its own name — so one shared config serves several agents
 * side by side. The legacy top-level `harness` key only selects the backfill's session formatter.
 *
 * SECURITY: the project-local file lives inside whatever repo the developer opens, so it is untrusted
 * input. It may set per-repo bank settings, but it must NOT be able to redirect where the user's
 * credential and prompts/transcripts are sent — otherwise a malicious repo could drop a
 * `.hindsight/coding-agent.json` that points `apiUrl` at its own server while the user-global
 * `apiToken` rides along in the Authorization header (token + prompt exfiltration on open).
 * `PROJECT_UNTRUSTED_KEYS` are therefore stripped from the project layer (see sanitizeProjectLayer).
 */
import { readFileSync } from "node:fs";
import { homedir } from "node:os";
import { dirname, join, resolve } from "node:path";
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
const PROJECT_UNTRUSTED_KEYS: readonly (keyof RawConfig)[] = [
  "apiUrl",
  "apiToken",
  "directoryBankMap",
];

/** Drop the untrusted keys from a project-local layer — from its top level AND its per-harness
 *  sections (so a repo can't smuggle apiUrl in under `harnesses.claude-code`). Warns once if any were
 *  present. Never mutates the input. */
function sanitizeProjectLayer(raw: RawConfig): RawConfig {
  const stripped = new Set<string>();
  const strip = <T extends Record<string, unknown>>(obj: T): T => {
    const out = { ...obj };
    for (const k of PROJECT_UNTRUSTED_KEYS) {
      if (k in out) {
        delete (out as Record<string, unknown>)[k];
        stripped.add(k);
      }
    }
    return out;
  };
  const clean = strip(raw as Record<string, unknown>) as RawConfig;
  if (clean.harnesses) {
    clean.harnesses = Object.fromEntries(
      Object.entries(clean.harnesses).map(([h, sec]) => [h, strip(sec as Record<string, unknown>)])
    ) as RawConfig["harnesses"];
  }
  if (stripped.size) {
    console.error(
      `hindsight: ignoring security-sensitive key(s) [${[...stripped].join(", ")}] from a project-local ` +
        `.hindsight/coding-agent.json — set apiUrl/apiToken only in your user-global ~/.hindsight/coding-agent.json`
    );
  }
  return clean;
}

export interface LoadOptions {
  /** Which harness is asking ("opencode", "claude-code", ...) — applies its `harnesses.<name>` overrides. */
  harness?: string;
  /** Project directory — the NEAREST .hindsight/coding-agent.json at or above it layers over the global file. */
  projectDir?: string;
  /** Explicit global-config path (default ~/.hindsight/coding-agent.json). */
  path?: string;
}

/** Nearest project config at or above `dir`: walk up until a .hindsight/coding-agent.json exists. */
function findProjectConfig(dir: string): string | undefined {
  let d = dir;
  for (let i = 0; i < 64; i++) {
    const candidate = join(d, ".hindsight", "coding-agent.json");
    try {
      readFileSync(candidate);
      return candidate;
    } catch {
      /* keep walking */
    }
    const parent = dirname(d);
    if (parent === d) return undefined; // filesystem root
    d = parent;
  }
  return undefined;
}

/** Apply one config layer (its top level, then its `harnesses.<name>` override) over `raw`. */
function applyLayer(raw: RawConfig, layer: RawConfig, harness?: string): RawConfig {
  let out = mergeRaw(raw, layer);
  const perHarness = harness ? layer.harnesses?.[harness] : undefined;
  if (perHarness) out = mergeRaw(out, perHarness);
  return out;
}

/** Load + resolve config from the layered files. Missing files -> silent defaults. The user-global
 *  file is trusted; the project-local file (from the opened repo) is sanitized — see the module-level
 *  SECURITY note and PROJECT_UNTRUSTED_KEYS. */
export function loadConfig(opts: LoadOptions | string = {}): Config {
  const o: LoadOptions = typeof opts === "string" ? { path: opts } : opts; // legacy: loadConfig(path)

  // Layer 1-3: user-global config (TRUSTED — unrestricted).
  const globalPath = o.path ?? CONFIG_PATH;
  let raw = applyLayer({}, readRaw(globalPath), o.harness);

  // Layer 4-5: project-local config (UNTRUSTED — comes from the opened repo). Sanitized so a repo
  // can set its own per-repo bank but cannot redirect the API endpoint/token or the global bank map.
  // Skip it when the upward walk lands back on the user-global file itself (a repo under $HOME with no
  // closer .hindsight config) — that file is already applied as the trusted layer, and re-applying it
  // as "untrusted" would strip its own apiUrl/apiToken and emit a spurious warning every session.
  const projectPath = o.projectDir ? findProjectConfig(o.projectDir) : undefined;
  if (projectPath && resolve(projectPath) !== resolve(globalPath)) {
    raw = applyLayer(raw, sanitizeProjectLayer(readRaw(projectPath)), o.harness);
  }

  return resolveConfig(raw);
}
