import { afterEach, describe, expect, it, vi } from "vitest";
import { mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync, existsSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { INSTALLERS, MARKER, run, type InstallCtx } from "./installer";

// Every test gets a FRESH temp dir as ctx.home (never the real $HOME) and a stubbed
// claudeMcp so the real `claude` CLI is never executed. run() is always called with
// explicit harness names so detect() (which probes PATH) never runs.

const homes: string[] = [];

function makeCtx(): InstallCtx & { claudeMcp: ReturnType<typeof vi.fn> } {
  const home = mkdtempSync(join(tmpdir(), "hindsight-installer-test-"));
  homes.push(home);
  const pkgRoot = join("/opt", MARKER); // contains the marker, like the real package path
  return { home, pkgRoot, dist: join(pkgRoot, "dist"), claudeMcp: vi.fn(() => true) };
}

function readJson(path: string): Record<string, any> {
  return JSON.parse(readFileSync(path, "utf8"));
}

function writeJsonAt(path: string, value: unknown): void {
  mkdirSync(dirname(path), { recursive: true });
  writeFileSync(path, JSON.stringify(value, null, 2) + "\n");
}

afterEach(() => {
  while (homes.length) rmSync(homes.pop()!, { recursive: true, force: true });
  vi.clearAllMocks();
});

describe("claude-code installer", () => {
  const settingsPath = (ctx: InstallCtx) => join(ctx.home, ".claude", "settings.json");

  it("install writes the 3 hook events with our dist commands and timeouts 30/30/60", () => {
    const ctx = makeCtx();
    expect(run(["install", "claude-code"], ctx)).toBe(0);
    const settings = readJson(settingsPath(ctx));
    const hooks = settings.hooks;
    expect(Object.keys(hooks).sort()).toEqual(["SessionStart", "Stop", "UserPromptSubmit"]);
    const inner = (ev: string) => hooks[ev][0].hooks[0];
    expect(inner("SessionStart").command).toContain(join(ctx.dist, "claude-sessionstart-hook.js"));
    expect(inner("UserPromptSubmit").command).toContain(join(ctx.dist, "claude-hook.js"));
    expect(inner("Stop").command).toContain(join(ctx.dist, "claude-stop-hook.js"));
    expect(inner("SessionStart").timeout).toBe(30);
    expect(inner("UserPromptSubmit").timeout).toBe(30);
    expect(inner("Stop").timeout).toBe(60);
    for (const ev of ["SessionStart", "UserPromptSubmit", "Stop"]) {
      expect(inner(ev).type).toBe("command");
    }
  });

  it("preserves pre-existing foreign hook entries and appends ours", () => {
    const ctx = makeCtx();
    const foreign = { hooks: [{ type: "command", command: "echo other-tool", timeout: 5 }] };
    writeJsonAt(settingsPath(ctx), { hooks: { SessionStart: [foreign] } });
    run(["install", "claude-code"], ctx);
    const events = readJson(settingsPath(ctx)).hooks.SessionStart;
    expect(events).toHaveLength(2);
    expect(events[0]).toEqual(foreign);
    expect(JSON.stringify(events[1])).toContain(MARKER);
  });

  it("re-install is idempotent — exactly ONE of our entries per event", () => {
    const ctx = makeCtx();
    run(["install", "claude-code"], ctx);
    run(["install", "claude-code"], ctx);
    const hooks = readJson(settingsPath(ctx)).hooks;
    for (const ev of ["SessionStart", "UserPromptSubmit", "Stop"]) {
      const ours = hooks[ev].filter((e: unknown) => JSON.stringify(e).includes(MARKER));
      expect(ours).toHaveLength(1);
      expect(hooks[ev]).toHaveLength(1);
    }
  });

  it("registers the MCP server via `claude mcp add` (user scope)", () => {
    const ctx = makeCtx();
    run(["install", "claude-code"], ctx);
    expect(ctx.claudeMcp).toHaveBeenCalledWith([
      "mcp",
      "add",
      "--scope",
      "user",
      "hindsight",
      "--",
      "node",
      join(ctx.dist, "mcp-server.js"),
    ]);
  });

  it("still succeeds when claudeMcp reports the CLI is unusable (manual instructions)", () => {
    const ctx = makeCtx();
    ctx.claudeMcp.mockReturnValue(false);
    const logs: string[] = [];
    ctx.log = (m) => logs.push(m);
    expect(run(["install", "claude-code"], ctx)).toBe(0);
    expect(logs.join("\n")).toContain("claude mcp add");
    // hooks were still written despite the MCP failure
    expect(existsSync(settingsPath(ctx))).toBe(true);
  });

  it("uninstall strips our entries, keeps foreign ones, and calls `claude mcp remove`", () => {
    const ctx = makeCtx();
    const foreign = { hooks: [{ type: "command", command: "echo other-tool", timeout: 5 }] };
    writeJsonAt(settingsPath(ctx), { hooks: { Stop: [foreign] } });
    run(["install", "claude-code"], ctx);
    run(["uninstall", "claude-code"], ctx);
    const settings = readJson(settingsPath(ctx));
    expect(settings.hooks.Stop).toEqual([foreign]);
    expect(settings.hooks.SessionStart).toBeUndefined();
    expect(settings.hooks.UserPromptSubmit).toBeUndefined();
    expect(JSON.stringify(settings)).not.toContain(MARKER);
    expect(ctx.claudeMcp).toHaveBeenCalledWith(["mcp", "remove", "--scope", "user", "hindsight"]);
  });

  it("uninstall removes the hooks object entirely when nothing else remains", () => {
    const ctx = makeCtx();
    run(["install", "claude-code"], ctx);
    run(["uninstall", "claude-code"], ctx);
    expect(readJson(settingsPath(ctx)).hooks).toBeUndefined();
  });
});

describe("codex installer", () => {
  const hooksPath = (ctx: InstallCtx) => join(ctx.home, ".codex", "hooks.json");
  const tomlPath = (ctx: InstallCtx) => join(ctx.home, ".codex", "config.toml");

  it("install writes the 3 hook events into hooks.json", () => {
    const ctx = makeCtx();
    expect(run(["install", "codex"], ctx)).toBe(0);
    const hooks = readJson(hooksPath(ctx)).hooks;
    expect(Object.keys(hooks).sort()).toEqual(["SessionStart", "Stop", "UserPromptSubmit"]);
    expect(hooks.SessionStart[0].hooks[0].command).toContain("codex-sessionstart-hook.js");
    expect(hooks.UserPromptSubmit[0].hooks[0].command).toContain("codex-hook.js");
    expect(hooks.Stop[0].hooks[0].command).toContain("codex-stop-hook.js");
  });

  it("creates config.toml with the features flag and mcp_servers section when missing", () => {
    const ctx = makeCtx();
    run(["install", "codex"], ctx);
    const toml = readFileSync(tomlPath(ctx), "utf8");
    expect(toml).toContain("[features]\ncodex_hooks = true");
    expect(toml).toContain("[mcp_servers.hindsight]");
    expect(toml).toContain(join(ctx.dist, "mcp-server.js"));
  });

  it("does NOT duplicate an existing [features] section (only appends mcp) and backs up the toml", () => {
    const ctx = makeCtx();
    const original = "[features]\nsome_flag = true\n";
    mkdirSync(join(ctx.home, ".codex"), { recursive: true });
    writeFileSync(tomlPath(ctx), original);
    run(["install", "codex"], ctx);
    const toml = readFileSync(tomlPath(ctx), "utf8");
    expect(toml.match(/^\[features\]/gm)).toHaveLength(1);
    expect(toml).not.toContain("codex_hooks = true"); // user is told to add it manually
    expect(toml).toContain("[mcp_servers.hindsight]");
    expect(readFileSync(`${tomlPath(ctx)}.hindsight-backup`, "utf8")).toBe(original);
  });

  it("appends nothing features-related when codex_hooks is already present", () => {
    const ctx = makeCtx();
    mkdirSync(join(ctx.home, ".codex"), { recursive: true });
    writeFileSync(tomlPath(ctx), "[features]\ncodex_hooks = true\n");
    run(["install", "codex"], ctx);
    const toml = readFileSync(tomlPath(ctx), "utf8");
    expect(toml.match(/codex_hooks/g)).toHaveLength(1);
    expect(toml.match(/^\[features\]/gm)).toHaveLength(1);
    expect(toml).toContain("[mcp_servers.hindsight]");
  });

  it("uninstall removes the mcp_servers.hindsight block and leaves the rest of the toml", () => {
    const ctx = makeCtx();
    run(["install", "codex"], ctx);
    run(["uninstall", "codex"], ctx);
    const toml = readFileSync(tomlPath(ctx), "utf8");
    expect(toml).not.toContain("[mcp_servers.hindsight]");
    expect(toml).toContain("codex_hooks = true"); // flag deliberately left in place
    const hooks = readJson(hooksPath(ctx)).hooks;
    expect(Object.keys(hooks)).toHaveLength(0);
  });
});

describe("gemini installer", () => {
  const settingsPath = (ctx: InstallCtx) => join(ctx.home, ".gemini", "settings.json");

  it("install writes SessionStart/BeforeAgent/SessionEnd (ms timeouts) plus mcpServers.hindsight", () => {
    const ctx = makeCtx();
    expect(run(["install", "gemini"], ctx)).toBe(0);
    const settings = readJson(settingsPath(ctx));
    const inner = (ev: string) => settings.hooks[ev][0].hooks[0];
    expect(inner("SessionStart").command).toContain("gemini-sessionstart-hook.js");
    expect(inner("BeforeAgent").command).toContain("gemini-hook.js");
    expect(inner("SessionEnd").command).toContain("gemini-stop-hook.js");
    expect(inner("SessionStart").timeout).toBe(15000);
    expect(inner("BeforeAgent").timeout).toBe(15000);
    expect(inner("SessionEnd").timeout).toBe(30000);
    expect(settings.mcpServers.hindsight).toEqual({
      command: "node",
      args: [join(ctx.dist, "mcp-server.js")],
      env: { HINDSIGHT_MCP_HARNESS: "gemini" },
    });
  });

  it("preserves existing unrelated settings keys", () => {
    const ctx = makeCtx();
    writeJsonAt(settingsPath(ctx), { auth: { selectedType: "oauth" }, theme: "dark" });
    run(["install", "gemini"], ctx);
    const settings = readJson(settingsPath(ctx));
    expect(settings.auth).toEqual({ selectedType: "oauth" });
    expect(settings.theme).toBe("dark");
  });

  it("uninstall removes only our hooks and mcp server", () => {
    const ctx = makeCtx();
    writeJsonAt(settingsPath(ctx), {
      auth: { selectedType: "oauth" },
      mcpServers: { other: { command: "other-tool" } },
    });
    run(["install", "gemini"], ctx);
    run(["uninstall", "gemini"], ctx);
    const settings = readJson(settingsPath(ctx));
    expect(settings.hooks).toBeUndefined();
    expect(settings.mcpServers.hindsight).toBeUndefined();
    expect(settings.mcpServers.other).toEqual({ command: "other-tool" });
    expect(settings.auth).toEqual({ selectedType: "oauth" });
    expect(JSON.stringify(settings)).not.toContain(MARKER);
  });
});

describe("opencode installer", () => {
  const cfgPath = (ctx: InstallCtx) => join(ctx.home, ".config", "opencode", "opencode.json");

  it("install adds ctx.pkgRoot to the plugin array exactly once, even across reinstalls", () => {
    const ctx = makeCtx();
    expect(run(["install", "opencode"], ctx)).toBe(0);
    run(["install", "opencode"], ctx);
    const cfg = readJson(cfgPath(ctx));
    expect(cfg.plugin).toEqual([ctx.pkgRoot]);
  });

  it("preserves other plugin entries", () => {
    const ctx = makeCtx();
    writeJsonAt(cfgPath(ctx), { plugin: ["some-other-plugin"] });
    run(["install", "opencode"], ctx);
    expect(readJson(cfgPath(ctx)).plugin).toEqual(["some-other-plugin", ctx.pkgRoot]);
  });

  it("uninstall removes our entry and deletes the plugin key when empty", () => {
    const ctx = makeCtx();
    run(["install", "opencode"], ctx);
    run(["uninstall", "opencode"], ctx);
    expect(readJson(cfgPath(ctx)).plugin).toBeUndefined();
  });

  it("uninstall keeps the plugin key when other entries remain", () => {
    const ctx = makeCtx();
    writeJsonAt(cfgPath(ctx), { plugin: ["some-other-plugin"] });
    run(["install", "opencode"], ctx);
    run(["uninstall", "opencode"], ctx);
    expect(readJson(cfgPath(ctx)).plugin).toEqual(["some-other-plugin"]);
  });
});

describe("cursor-cli installer", () => {
  const hooksPath = (ctx: InstallCtx) => join(ctx.home, ".cursor", "hooks.json");
  const mcpPath = (ctx: InstallCtx) => join(ctx.home, ".cursor", "mcp.json");

  it("install writes the beforeSubmitPrompt hook and the mcp.json server entry", () => {
    const ctx = makeCtx();
    expect(run(["install", "cursor-cli"], ctx)).toBe(0);
    const hooks = readJson(hooksPath(ctx)).hooks;
    expect(hooks.beforeSubmitPrompt).toHaveLength(1);
    expect(hooks.beforeSubmitPrompt[0].command).toContain(join(ctx.dist, "cursor-hook.js"));
    const mcp = readJson(mcpPath(ctx));
    expect(mcp.mcpServers.hindsight).toEqual({
      command: "node",
      args: [join(ctx.dist, "mcp-server.js")],
    });
  });

  it("uninstall cleans both files", () => {
    const ctx = makeCtx();
    run(["install", "cursor-cli"], ctx);
    run(["uninstall", "cursor-cli"], ctx);
    expect(readJson(hooksPath(ctx)).hooks).toBeUndefined();
    expect(readJson(mcpPath(ctx)).mcpServers.hindsight).toBeUndefined();
  });
});

describe("run() CLI behavior", () => {
  it("returns 1 for an unknown harness name and touches nothing", () => {
    const ctx = makeCtx();
    const logs: string[] = [];
    ctx.log = (m) => logs.push(m);
    expect(run(["install", "not-a-harness"], ctx)).toBe(1);
    expect(logs.join("\n")).toContain('unknown harness "not-a-harness"');
    expect(existsSync(join(ctx.home, ".claude"))).toBe(false);
    expect(ctx.claudeMcp).not.toHaveBeenCalled();
  });

  it("returns 0 with usage when no command is given", () => {
    const ctx = makeCtx();
    const logs: string[] = [];
    ctx.log = (m) => logs.push(m);
    expect(run([], ctx)).toBe(0);
    expect(logs.join("\n")).toContain("usage:");
  });

  it("returns 1 for an unknown command", () => {
    const ctx = makeCtx();
    expect(run(["frobnicate"], ctx)).toBe(1);
  });

  it("explicit harness names bypass detection — installs into an empty home", () => {
    const ctx = makeCtx();
    // nothing pre-exists in this fresh home, yet the named harness installs fine
    expect(run(["install", "gemini", "opencode"], ctx)).toBe(0);
    expect(existsSync(join(ctx.home, ".gemini", "settings.json"))).toBe(true);
    expect(existsSync(join(ctx.home, ".config", "opencode", "opencode.json"))).toBe(true);
  });

  it("first write to a pre-existing json creates <file>.hindsight-backup with the original content", () => {
    const ctx = makeCtx();
    const path = join(ctx.home, ".gemini", "settings.json");
    writeJsonAt(path, { auth: { selectedType: "oauth" } });
    const original = readFileSync(path, "utf8");
    run(["install", "gemini"], ctx);
    run(["install", "gemini"], ctx); // second write must NOT overwrite the backup
    expect(readFileSync(`${path}.hindsight-backup`, "utf8")).toBe(original);
  });

  it("exposes the five expected harnesses", () => {
    expect(INSTALLERS.map((i) => i.name)).toEqual([
      "opencode",
      "claude-code",
      "codex",
      "gemini",
      "cursor-cli",
    ]);
  });
});
