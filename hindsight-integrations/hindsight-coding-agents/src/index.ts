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
import { applyBankConfig, loadConfig } from "./core/config";
import { log } from "./core/log";
import { HindsightClient } from "./core/hindsight";
import { RuntimeCore } from "./core/runtime";
import { opencodeAdapter } from "./harness/opencode";

const HindsightCodingAgentsPlugin: Plugin = async (input) => {
  // This entry is loaded BY opencode, so the harness is known — not chosen by config. Per-agent
  // settings come from the config's `harnesses.opencode` section (and a project-local file, if any).
  const projectDir = input?.worktree || input?.directory;
  let cfg = loadConfig({ harness: "opencode" });
  if (cfg.disabled) return {}; // inert: same agent, no memory (baseline parity)

  const resolved = applyBankConfig(cfg, deriveBankId(cfg, projectDir || process.cwd(), "opencode"));
  cfg = resolved.cfg;
  const bankId = resolved.bankId;
  if (cfg.disabled) return {}; // per-bank opt-out (banks.<id> override)
  const client = new HindsightClient({ apiUrl: cfg.apiUrl, apiToken: cfg.apiToken, bank: bankId });
  const core = new RuntimeCore(client, bankId, cfg);
  // Visible presence via opencode's own notice API (POST /tui/show-toast) — never stderr.
  const oc = (input as { client?: { tui?: { showToast?: (o: unknown) => Promise<unknown> } } })
    ?.client;
  // opencode injects the v1 SDK client: showToast takes {body: {...}} and RESOLVES with
  // {data|error} (openapi-fetch style) instead of rejecting — inspect the result, not .catch.
  log.debug("opencode", "toast channel", { wired: Boolean(oc?.tui?.showToast) });
  if (oc?.tui?.showToast) {
    // The toast event is not durable: one published before the TUI mounts and subscribes to the
    // event stream is silently lost. A warm bank makes the seed banner fire <1s after plugin init,
    // well before mount — so hold any toast until ~3s past init (later toasts go out immediately).
    const initAt = Date.now();
    core.setNotifier((title, message) => {
      const wait = Math.max(0, 3000 - (Date.now() - initAt));
      setTimeout(() => {
        void oc.tui!.showToast!({ body: { title, message, variant: "info", duration: 6000 } })
          .then((r: unknown) => {
            const err = (r as { error?: unknown })?.error;
            if (err)
              log.debug("opencode", "toast rejected", { error: JSON.stringify(err).slice(0, 200) });
          })
          .catch((e: unknown) =>
            log.debug("opencode", "toast failed", { error: String(e).slice(0, 120) })
          );
      }, wait).unref?.();
    });
  }

  const runtime = opencodeAdapter.createRuntime(core) as Awaited<ReturnType<Plugin>>;

  // SessionStart-equivalent: cold-check the bank, kick off the background engine, compute the
  // knowledge preamble. Fire-and-forget — opencode BLOCKS ITS BOOT on plugin init, so this must
  // never gate startup on a network round-trip (a stalled server froze the whole TUI). onPrompt
  // already tolerates an empty preamble until this resolves.
  void core.seedIfCold(projectDir);

  // Keeping the bank current needs no separate path: seedIfCold fired the deepen engine, and the
  // engine's idempotent git pass (cfg.gitIngest) ingests whatever is new — syncing IS re-seeding.
  return runtime;
};

export default HindsightCodingAgentsPlugin;
export { HindsightCodingAgentsPlugin };
