#!/usr/bin/env node
/**
 * hindsight-coding-agents install|uninstall [harness...]
 *
 * ONE setup command for every supported coding agent. With no harness arguments it detects which
 * agents exist on this machine (binary on PATH or config dir present) and wires each one's NATIVE
 * integration — hooks + MCP where the host wants them:
 *
 *   opencode     add this package to `plugin` in ~/.config/opencode/opencode.json
 *   claude-code  3 hooks in ~/.claude/settings.json + `claude mcp add` (user scope)
 *   codex        3 hooks in ~/.codex/hooks.json + [features]/[mcp_servers] in config.toml
 *   gemini       3 hooks + mcpServers in ~/.gemini/settings.json
 *   cursor-cli   beforeSubmitPrompt hook in ~/.cursor/hooks.json + ~/.cursor/mcp.json
 *
 * IDEMPOTENT: our entries are recognized by the package path in their command ("hindsight-coding-
 * agents"), replaced on re-install (so moving the package just needs `install` again) and removed
 * on `uninstall`. Everything else in the host's config files is preserved; files are created when
 * missing. Backups: the first time we touch an existing file we write `<file>.hindsight-backup`.
 */
import { execFileSync } from "node:child_process";
import { copyFileSync, existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { homedir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

export const MARKER = "hindsight-coding-agents";

export interface InstallCtx {
  home: string;
  pkgRoot: string; // package root (opencode plugin entry)
  dist: string; // built entry points
  /** Runs `claude mcp ...`; injectable for tests. Returns false when the CLI isn't usable. */
  claudeMcp?: (args: string[]) => boolean;
  log?: (m: string) => void;
}

function readJson(path: string): Record<string, any> {
  try {
    return JSON.parse(readFileSync(path, "utf8"));
  } catch {
    return {};
  }
}

function writeJson(path: string, value: unknown): void {
  mkdirSync(dirname(path), { recursive: true });
  if (existsSync(path) && !existsSync(`${path}.hindsight-backup`)) {
    copyFileSync(path, `${path}.hindsight-backup`);
  }
  writeFileSync(path, JSON.stringify(value, null, 2) + "\n");
}

/** Hook-array merge for claude/codex-style files: drop our old entries, append the new one. */
function mergeHookEvent(existing: any[] | undefined, entry: unknown): any[] {
  const kept = (existing ?? []).filter(
    (e) => !JSON.stringify(e).includes(MARKER)
  );
  return [...kept, entry];
}

function stripOurs(existing: any[] | undefined): any[] {
  return (existing ?? []).filter((e) => !JSON.stringify(e).includes(MARKER));
}

/** Remove the event key entirely when nothing else remains (leave the host file tidy). */
function setOrDelete(obj: Record<string, any>, key: string, arr: any[]): void {
  if (arr.length) obj[key] = arr;
  else delete obj[key];
}

const cmdHook = (dist: string, file: string, timeout: number) => ({
  hooks: [{ type: "command", command: `node "${join(dist, file)}"`, timeout }],
});

// ── per-harness adapters ────────────────────────────────────────────────────────

export interface HarnessInstaller {
  name: string;
  detect(ctx: InstallCtx): boolean;
  install(ctx: InstallCtx): void;
  uninstall(ctx: InstallCtx): void;
}

function onPath(bin: string): boolean {
  try {
    execFileSync("which", [bin], { stdio: "pipe" });
    return true;
  } catch {
    return false;
  }
}

const opencode: HarnessInstaller = {
  name: "opencode",
  detect: (c) => onPath("opencode") || existsSync(join(c.home, ".config", "opencode")),
  install(c) {
    const path = join(c.home, ".config", "opencode", "opencode.json");
    const cfg = readJson(path);
    const plugins: string[] = Array.isArray(cfg.plugin) ? cfg.plugin : [];
    cfg.plugin = [...plugins.filter((p) => !String(p).includes(MARKER)), c.pkgRoot];
    writeJson(path, cfg);
    c.log?.(`opencode: plugin registered in ${path}`);
  },
  uninstall(c) {
    const path = join(c.home, ".config", "opencode", "opencode.json");
    if (!existsSync(path)) return;
    const cfg = readJson(path);
    if (Array.isArray(cfg.plugin)) {
      cfg.plugin = cfg.plugin.filter((p: string) => !String(p).includes(MARKER));
      if (!cfg.plugin.length) delete cfg.plugin;
      writeJson(path, cfg);
    }
    c.log?.("opencode: plugin entry removed");
  },
};

const claudeCode: HarnessInstaller = {
  name: "claude-code",
  detect: (c) => onPath("claude") || existsSync(join(c.home, ".claude")),
  install(c) {
    const path = join(c.home, ".claude", "settings.json");
    const settings = readJson(path);
    settings.hooks = settings.hooks ?? {};
    settings.hooks.SessionStart = mergeHookEvent(
      settings.hooks.SessionStart,
      cmdHook(c.dist, "claude-sessionstart-hook.js", 30)
    );
    settings.hooks.UserPromptSubmit = mergeHookEvent(
      settings.hooks.UserPromptSubmit,
      cmdHook(c.dist, "claude-hook.js", 30)
    );
    settings.hooks.Stop = mergeHookEvent(settings.hooks.Stop, cmdHook(c.dist, "claude-stop-hook.js", 60));
    writeJson(path, settings);
    c.log?.(`claude-code: hooks merged into ${path}`);
    const mcp = c.claudeMcp ?? defaultClaudeMcp;
    if (mcp(["mcp", "add", "--scope", "user", "hindsight", "--", "node", join(c.dist, "mcp-server.js")])) {
      c.log?.("claude-code: MCP server registered (claude mcp add, user scope)");
    } else {
      c.log?.(
        `claude-code: could not run \`claude mcp add\` — register the tools manually:\n` +
          `  claude mcp add --scope user hindsight -- node "${join(c.dist, "mcp-server.js")}"`
      );
    }
  },
  uninstall(c) {
    const path = join(c.home, ".claude", "settings.json");
    if (existsSync(path)) {
      const settings = readJson(path);
      if (settings.hooks) {
        for (const ev of ["SessionStart", "UserPromptSubmit", "Stop"]) {
          setOrDelete(settings.hooks, ev, stripOurs(settings.hooks[ev]));
        }
        if (!Object.keys(settings.hooks).length) delete settings.hooks;
        writeJson(path, settings);
      }
    }
    const mcp = c.claudeMcp ?? defaultClaudeMcp;
    mcp(["mcp", "remove", "--scope", "user", "hindsight"]);
    c.log?.("claude-code: hooks + MCP registration removed");
  },
};

function defaultClaudeMcp(args: string[]): boolean {
  try {
    execFileSync("claude", args, { stdio: "pipe" });
    return true;
  } catch {
    return false;
  }
}

const codex: HarnessInstaller = {
  name: "codex",
  detect: (c) => onPath("codex") || existsSync(join(c.home, ".codex")),
  install(c) {
    const hooksPath = join(c.home, ".codex", "hooks.json");
    const cfg = readJson(hooksPath);
    cfg.hooks = cfg.hooks ?? {};
    cfg.hooks.SessionStart = mergeHookEvent(
      cfg.hooks.SessionStart,
      cmdHook(c.dist, "codex-sessionstart-hook.js", 30)
    );
    cfg.hooks.UserPromptSubmit = mergeHookEvent(cfg.hooks.UserPromptSubmit, cmdHook(c.dist, "codex-hook.js", 30));
    cfg.hooks.Stop = mergeHookEvent(cfg.hooks.Stop, cmdHook(c.dist, "codex-stop-hook.js", 60));
    writeJson(hooksPath, cfg);
    c.log?.(`codex: hooks merged into ${hooksPath}`);

    // config.toml: append-only, never rewrite (TOML round-tripping is not worth the risk).
    const tomlPath = join(c.home, ".codex", "config.toml");
    let toml = existsSync(tomlPath) ? readFileSync(tomlPath, "utf8") : "";
    const additions: string[] = [];
    if (!/^\s*codex_hooks\s*=/m.test(toml)) {
      if (/^\[features\]/m.test(toml)) {
        c.log?.("codex: add `codex_hooks = true` under your existing [features] section in ~/.codex/config.toml");
      } else {
        additions.push("[features]\ncodex_hooks = true");
      }
    }
    if (!toml.includes("[mcp_servers.hindsight]")) {
      additions.push(`[mcp_servers.hindsight]\ncommand = "node"\nargs = ["${join(c.dist, "mcp-server.js")}"]`);
    }
    if (additions.length) {
      if (existsSync(tomlPath) && !existsSync(`${tomlPath}.hindsight-backup`)) {
        copyFileSync(tomlPath, `${tomlPath}.hindsight-backup`);
      }
      mkdirSync(dirname(tomlPath), { recursive: true });
      writeFileSync(tomlPath, `${toml.replace(/\n*$/, "\n\n")}${additions.join("\n\n")}\n`);
      c.log?.(`codex: appended ${additions.length} section(s) to ${tomlPath}`);
    }
  },
  uninstall(c) {
    const hooksPath = join(c.home, ".codex", "hooks.json");
    if (existsSync(hooksPath)) {
      const cfg = readJson(hooksPath);
      if (cfg.hooks) {
        for (const ev of ["SessionStart", "UserPromptSubmit", "Stop"]) {
          setOrDelete(cfg.hooks, ev, stripOurs(cfg.hooks[ev]));
        }
        writeJson(hooksPath, cfg);
      }
    }
    const tomlPath = join(c.home, ".codex", "config.toml");
    if (existsSync(tomlPath)) {
      const toml = readFileSync(tomlPath, "utf8");
      const cleaned = toml.replace(/\n?\[mcp_servers\.hindsight\]\ncommand = "node"\nargs = \[[^\]]*\]\n?/g, "\n");
      if (cleaned !== toml) writeFileSync(tomlPath, cleaned);
    }
    c.log?.("codex: hooks + MCP section removed (codex_hooks flag left as-is — other hooks may use it)");
  },
};

const gemini: HarnessInstaller = {
  name: "gemini",
  detect: (c) => onPath("gemini") || existsSync(join(c.home, ".gemini")),
  install(c) {
    const path = join(c.home, ".gemini", "settings.json");
    const settings = readJson(path);
    settings.hooks = settings.hooks ?? {};
    // Gemini hook timeouts are MILLISECONDS (unlike claude/codex seconds).
    settings.hooks.SessionStart = mergeHookEvent(
      settings.hooks.SessionStart,
      cmdHook(c.dist, "gemini-sessionstart-hook.js", 15000)
    );
    settings.hooks.BeforeAgent = mergeHookEvent(settings.hooks.BeforeAgent, cmdHook(c.dist, "gemini-hook.js", 15000));
    settings.hooks.SessionEnd = mergeHookEvent(
      settings.hooks.SessionEnd,
      cmdHook(c.dist, "gemini-stop-hook.js", 30000)
    );
    settings.mcpServers = {
      ...(settings.mcpServers ?? {}),
      hindsight: {
        command: "node",
        args: [join(c.dist, "mcp-server.js")],
        env: { HINDSIGHT_MCP_HARNESS: "gemini" },
      },
    };
    writeJson(path, settings);
    c.log?.(`gemini: hooks + mcpServers merged into ${path}`);
  },
  uninstall(c) {
    const path = join(c.home, ".gemini", "settings.json");
    if (!existsSync(path)) return;
    const settings = readJson(path);
    if (settings.hooks) {
      for (const ev of ["SessionStart", "BeforeAgent", "SessionEnd"]) {
        setOrDelete(settings.hooks, ev, stripOurs(settings.hooks[ev]));
      }
      if (!Object.keys(settings.hooks).length) delete settings.hooks;
    }
    if (settings.mcpServers?.hindsight) delete settings.mcpServers.hindsight;
    writeJson(path, settings);
    c.log?.("gemini: hooks + mcpServers.hindsight removed");
  },
};

const cursor: HarnessInstaller = {
  name: "cursor-cli",
  detect: (c) => onPath("cursor-agent") || existsSync(join(c.home, ".cursor")),
  install(c) {
    const hooksPath = join(c.home, ".cursor", "hooks.json");
    const cfg = readJson(hooksPath);
    cfg.hooks = cfg.hooks ?? {};
    cfg.hooks.beforeSubmitPrompt = mergeHookEvent(cfg.hooks.beforeSubmitPrompt, {
      command: `node "${join(c.dist, "cursor-hook.js")}"`,
    });
    writeJson(hooksPath, cfg);
    const mcpPath = join(c.home, ".cursor", "mcp.json");
    const mcp = readJson(mcpPath);
    mcp.mcpServers = { ...(mcp.mcpServers ?? {}), hindsight: { command: "node", args: [join(c.dist, "mcp-server.js")] } };
    writeJson(mcpPath, mcp);
    c.log?.(`cursor-cli: hook merged into ${hooksPath}, MCP into ${mcpPath}`);
  },
  uninstall(c) {
    const hooksPath = join(c.home, ".cursor", "hooks.json");
    if (existsSync(hooksPath)) {
      const cfg = readJson(hooksPath);
      if (cfg.hooks) {
        setOrDelete(cfg.hooks, "beforeSubmitPrompt", stripOurs(cfg.hooks.beforeSubmitPrompt));
        if (!Object.keys(cfg.hooks).length) delete cfg.hooks;
        writeJson(hooksPath, cfg);
      }
    }
    const mcpPath = join(c.home, ".cursor", "mcp.json");
    if (existsSync(mcpPath)) {
      const mcp = readJson(mcpPath);
      if (mcp.mcpServers?.hindsight) {
        delete mcp.mcpServers.hindsight;
        writeJson(mcpPath, mcp);
      }
    }
    c.log?.("cursor-cli: hook + MCP entry removed");
  },
};

export const INSTALLERS: HarnessInstaller[] = [opencode, claudeCode, codex, gemini, cursor];

// ── CLI ─────────────────────────────────────────────────────────────────────────

export function run(argv: string[], ctx: InstallCtx): number {
  const [command, ...names] = argv;
  // The wiring we write is ABSOLUTE paths into this package's dist. From an npx/pnpm-dlx cache
  // those paths die on cache eviction — every hook silently stops. Refuse and say what to do.
  if (command === "install" && /\/(_npx|\.npm\/_npx|dlx-)\/|\/_cacache\//.test(ctx.pkgRoot)) {
    ctx.log?.(
      "refusing to install from an npx/dlx cache: the hook wiring would point into a cache npm can " +
        "evict, silently breaking every session.\nInstall the package permanently, then re-run:\n" +
        "  npm install -g hindsight-coding-agents && hindsight-coding-agents install"
    );
    return 1;
  }
  if (command !== "install" && command !== "uninstall") {
    ctx.log?.(
      `usage: hindsight-coding-agents <install|uninstall> [harness...]\n` +
        `harnesses: ${INSTALLERS.map((i) => i.name).join(", ")} (default: every one detected on this machine)`
    );
    return command ? 1 : 0;
  }
  let targets: HarnessInstaller[];
  if (names.length) {
    targets = [];
    for (const n of names) {
      const hit = INSTALLERS.find((i) => i.name === n);
      if (!hit) {
        ctx.log?.(`unknown harness "${n}" — expected one of: ${INSTALLERS.map((i) => i.name).join(", ")}`);
        return 1;
      }
      targets.push(hit);
    }
  } else {
    targets = INSTALLERS.filter((i) => i.detect(ctx));
    if (!targets.length) {
      ctx.log?.("no supported coding agents detected — pass harness names explicitly");
      return 1;
    }
    ctx.log?.(`detected: ${targets.map((t) => t.name).join(", ")}`);
  }
  for (const t of targets) t[command](ctx);
  ctx.log?.(
    command === "install"
      ? `\n✅ installed. Configure the server in ~/.hindsight/coding-agent.json (apiUrl/apiToken) and start a session.`
      : `\n✅ uninstalled.`
  );
  return 0;
}

/* c8 ignore start */
const isMain = process.argv[1] && import.meta.url === new URL(`file://${process.argv[1]}`).href;
if (isMain || process.argv[1]?.endsWith("installer.js")) {
  const dist = dirname(fileURLToPath(import.meta.url));
  process.exit(
    run(process.argv.slice(2), {
      home: homedir(),
      pkgRoot: dirname(dist),
      dist,
      log: (m) => console.log(m),
    })
  );
}
/* c8 ignore stop */
