/**
 * hindsight-coding-agents — long-term memory for coding agents (recall + INJECT), harness-pluggable.
 *
 * This file is the opencode entrypoint: opencode loads the default export as a persistent Plugin.
 * opencode runs the SAME v2 surface as the Claude Code / Codex hook harnesses, just delivered
 * through opencode's plugin hooks instead of a fresh process per event:
 *   READ   — each user turn, recall on the prompt and PUSH a `<hindsight_memories>` block (with the
 *            attribution + user-feedback framing) into the system prompt.
 *   SEED   — on load, cold-check the bank and (if cold) start a background git-log seed + codebase
 *            survey, and compute the knowledge-page preamble injected on the session's first turn.
 *   TOOLS  — register the hindsight_* knowledge/recall suite natively (no MCP server needed).
 *   WRITE  — on by default: every few turns, upsert the rich transcript (text + tool calls/outputs).
 *
 * The recall/inject/seed/write-back logic is a harness-agnostic RuntimeCore; the opencode adapter
 * binds it to opencode's plugin API. All configuration comes from ~/.hindsight/coding-agent.json
 * (no environment variables) — see core/config.ts for the shape and defaults.
 */
import type { Plugin } from "@opencode-ai/plugin";
import { deriveBankId } from "./core/bank";
import { loadConfig } from "./core/config";
import { HindsightClient } from "./core/hindsight";
import { RuntimeCore } from "./core/runtime";
import { opencodeAdapter } from "./harness/opencode";

const HindsightCodingAgentsPlugin: Plugin = async (input) => {
  // This entry is loaded BY opencode, so the harness is known — not chosen by config. Per-agent
  // settings come from the config's `harnesses.opencode` section (and a project-local file, if any).
  const projectDir = input?.worktree || input?.directory;
  const cfg = loadConfig({ harness: "opencode" });
  if (cfg.disabled) return {}; // inert: same agent, no memory (baseline parity)

  const bankId = deriveBankId(cfg, projectDir || process.cwd(), "opencode");
  const client = new HindsightClient({ apiUrl: cfg.apiUrl, apiToken: cfg.apiToken, bank: bankId });
  const core = new RuntimeCore(client, bankId, cfg);

  const runtime = opencodeAdapter.createRuntime(core) as Awaited<ReturnType<Plugin>>;

  // SessionStart-equivalent: cold-check the bank and (if cold) kick off the background seed, and
  // compute the knowledge preamble the first turn injects. Awaited so the preamble is ready before
  // the first prompt; it is internally best-effort and bounded by the client's timeouts.
  await core.seedIfCold(projectDir);

  // Keeping the bank current needs no separate path: seedIfCold fired the deepen engine, and the
  // engine's idempotent git pass (cfg.gitIngest) ingests whatever is new — syncing IS re-seeding.
  return runtime;
};

export default HindsightCodingAgentsPlugin;
export { HindsightCodingAgentsPlugin };
