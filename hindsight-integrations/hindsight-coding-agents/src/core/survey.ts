/**
 * Codebase survey (Option C): on a cold repo, `session-start.ts` spawns a DETACHED headless
 * `claude` alongside the git-history backfill (core/seed.ts) to explore the repo's structure and
 * ingest what it finds as memories, via the `agent_knowledge_ingest` MCP tool (core/knowledge-tools.ts).
 *
 * A Hindsight knowledge page's body is synthesized server-side from the bank's MEMORIES via its
 * `source_query` — it can't be authored directly. So this survey doesn't write pages itself; it
 * ingests structural findings as documents, and the existing pages (missions.ts PAGES) synthesize
 * from them on the next consolidation, same as git history and chat transcripts do.
 *
 * Fire-and-forget and fail-safe throughout, mirroring core/seed.ts's `startBackgroundSeed`: a
 * missing `claude` binary or a spawn failure must silently no-op, never crash the SessionStart hook.
 */
import { spawn as realSpawn } from "node:child_process";
import { existsSync } from "node:fs";
import { homedir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

/** Resolve the claude CLI binary for a detached spawn (a shell alias won't apply to child_process).
 *  Order: explicit arg -> env HINDSIGHT_CLAUDE_BIN -> ~/.claude/local/claude (native installer) ->
 *  "claude" (PATH fallback, resolved by the shell/child_process at spawn time). */
export function resolveClaudeBin(explicit?: string): string {
  if (explicit) return explicit;
  if (process.env.HINDSIGHT_CLAUDE_BIN) return process.env.HINDSIGHT_CLAUDE_BIN;
  const nativeInstallPath = join(homedir(), ".claude", "local", "claude");
  try {
    if (existsSync(nativeInstallPath)) return nativeInstallPath;
  } catch {
    /* fall through to PATH lookup */
  }
  return "claude";
}

export const SURVEY_PROMPT =
  "You are performing a one-time structural survey of THIS repository to seed its Hindsight " +
  "memory. Work efficiently — DO NOT read every file; sample enough to understand the " +
  "architecture: the directory layout, entry points, package manifests (package.json / " +
  "pyproject.toml / Cargo.toml / go.mod), the README, and a few representative source files per " +
  "major area.\n" +
  "Then use the agent_knowledge_ingest tool to save what you learned as separate documents (one " +
  "call each), with these titles and factual, developer-oriented content grounded in what you " +
  "actually saw:\n" +
  '- "Repository component map": the top-level modules/directories, each one\'s responsibility, ' +
  "and how they connect (data flow / dependencies).\n" +
  '- "Repository core concepts": the domain model and key abstractions a new contributor must ' +
  "understand.\n" +
  '- "Repository conventions and patterns": naming, file/module organization, testing approach, ' +
  "error handling, and other recurring patterns.\n" +
  '- "Repository tech stack and features": languages, frameworks, build/test tooling, and the ' +
  "notable features the project provides.\n" +
  "Keep each a few hundred words. When done, stop.";

/**
 * Tools the survey session must NEVER be able to invoke, no matter what a prompt injection in a
 * repo file (README, source comment, commit message, etc.) tries to talk it into. This is a
 * deny-list, not an allow-list gap: `--allowedTools` alone does not reliably block unlisted tools
 * in headless `-p` mode (empirically verified against `claude` v2.1.218 — the session had full
 * Bash/Write access despite `--allowedTools` omitting them, because `--permission-mode
 * bypassPermissions` overrides the allow-list). `--disallowedTools` DOES reliably block, with or
 * without a permission-mode flag, so it — not bypassPermissions — is the actual sandbox boundary
 * here. See survey.test.ts / the live acceptance test for the empirical verification.
 */
const SURVEY_DISALLOWED_TOOLS = [
  "Bash",
  "Write",
  "Edit",
  "NotebookEdit",
  "WebFetch",
  "WebSearch",
  "Task",
];

/** Spawn a DETACHED headless `claude` to survey `repoDir` and ingest structural findings via the
 *  `agent_knowledge_ingest` MCP tool. Fire-and-forget; never throws. */
export function startCodebaseSurvey(
  repoDir: string,
  opts: {
    model?: string;
    budgetUsd?: number;
    mcpServerPath?: string;
    claudeBin?: string;
    spawn?: typeof realSpawn;
  } = {}
): void {
  try {
    const spawnFn = opts.spawn ?? realSpawn;
    const mcpServerPath =
      opts.mcpServerPath ?? join(dirname(fileURLToPath(import.meta.url)), "mcp-server.js");
    const bin = resolveClaudeBin(opts.claudeBin);
    const mcpConfig = JSON.stringify({
      mcpServers: {
        hindsight: {
          command: "node",
          args: [mcpServerPath],
          env: { HINDSIGHT_MCP_PROJECT_CWD: repoDir },
        },
      },
    });
    const child = spawnFn(
      bin,
      [
        "-p",
        SURVEY_PROMPT,
        "--model",
        opts.model ?? "sonnet",
        "--mcp-config",
        mcpConfig,
        "--strict-mcp-config",
        "--allowedTools",
        "Read",
        "Glob",
        "Grep",
        "mcp__hindsight__agent_knowledge_ingest",
        "--disallowedTools",
        ...SURVEY_DISALLOWED_TOOLS,
        "--max-budget-usd",
        String(opts.budgetUsd ?? 0.5),
        // Deliberately NO --permission-mode: default headless mode + --disallowedTools blocks the
        // dangerous tools outright (no permission prompt, no hang) while still letting the allowed
        // read-only tools + the ingest MCP tool run unattended. bypassPermissions must NOT be used
        // here — it defeats --allowedTools, and (per the empirical test) the read-only tools work
        // fine without it.
      ],
      {
        cwd: repoDir,
        detached: true,
        stdio: "ignore",
        // Anti-recursion: the survey's own claude session must not fire our hooks (session-start,
        // UserPromptSubmit, Stop) — see the HINDSIGHT_DISABLE_HOOKS guard in hook.ts/retain-hook.ts/
        // session-start.ts.
        env: { ...process.env, HINDSIGHT_DISABLE_HOOKS: "1" },
      }
    );
    // spawn() failures (claude not found, EACCES, sandboxed environments) often arrive
    // ASYNCHRONOUSLY as an 'error' event on the child, not as a synchronous throw — an unhandled
    // 'error' event would crash the caller. Swallow it: fire-and-forget best-effort.
    child.on("error", () => {});
    child.unref();
  } catch {
    /* best-effort: a failed spawn must not break the caller */
  }
}
