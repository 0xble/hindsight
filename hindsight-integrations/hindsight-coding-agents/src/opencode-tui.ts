/**
 * opencode TUI-side companion (separate plugin entry — opencode loads `{ tui }` modules in the
 * TUI process, distinct from the server plugin). Restores VISIBLE Hindsight presence on opencode
 * the legitimate way: `api.ui.toast`, instead of the stderr prints that bled into the TUI layout.
 *
 *   - on activation: the session banner as an info toast (bank id + that memory is active)
 *   - reflect trail: watches the leveled plugin log and toasts the reflect goal/preview lines the
 *     server plugin writes there (once per session — low volume by construction)
 *
 * Registered by the installer as a second `plugin` entry pointing at dist/opencode-tui.js.
 */
import type { TuiPlugin } from "@opencode-ai/plugin/tui";
import { openSync, readSync, statSync } from "node:fs";
import { deriveBankId } from "./core/bank";
import { applyBankConfig, loadConfig } from "./core/config";
import { logFilePath } from "./core/log";

export const tui: TuiPlugin = async (api) => {
  let cfg;
  let bankId: string | undefined;
  try {
    const dir = process.cwd();
    const base = loadConfig({ harness: "opencode" });
    const resolved = applyBankConfig(base, deriveBankId(base, dir, "opencode"));
    cfg = resolved.cfg;
    bankId = resolved.bankId;
    if (cfg.disabled) return; // memory off for this repo — no presence to show
    api.ui.toast({
      variant: "info",
      title: "Hindsight",
      message: `Tracking this repo's decisions and history → memory bank “${bankId}”`,
      duration: 6000,
    });
  } catch {
    return; // presence is cosmetic — never break the TUI
  }

  // Reflect trail: poll the plugin log tail for the server plugin's "reflect goal" entries.
  let offset = 0;
  try {
    offset = statSync(logFilePath()).size; // start at EOF — only new lines
  } catch {
    /* log not created yet */
  }
  const timer = setInterval(() => {
    try {
      const file = logFilePath();
      const size = statSync(file).size;
      if (size <= offset) return;
      const fd = openSync(file, "r");
      const buf = Buffer.alloc(size - offset);
      readSync(fd, buf, 0, buf.length, offset);
      offset = size;
      for (const line of buf.toString("utf8").split("\n")) {
        if (!line.includes("reflect goal")) continue;
        const m = line.match(/\{.*\}$/);
        if (!m) continue;
        try {
          const { query, preview } = JSON.parse(m[0]) as { query?: string; preview?: string };
          api.ui.toast({
            variant: "info",
            title: "Hindsight · recalled past decisions",
            message: `goal: “${query ?? ""}”\n${(preview ?? "").slice(0, 160)}`,
            duration: 8000,
          });
        } catch {
          /* malformed line — skip */
        }
      }
    } catch {
      /* log unreadable — try again next tick */
    }
  }, 2000);
  api.lifecycle.onDispose(() => clearInterval(timer));
};

export default { id: "hindsight-tui", tui };
