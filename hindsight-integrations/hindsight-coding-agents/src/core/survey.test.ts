import { afterEach, describe, expect, it, vi } from "vitest";
import { resolveClaudeBin, startCodebaseSurvey, SURVEY_PROMPT } from "./survey";

describe("resolveClaudeBin", () => {
  const ORIGINAL_ENV = process.env.HINDSIGHT_CLAUDE_BIN;

  afterEach(() => {
    if (ORIGINAL_ENV === undefined) delete process.env.HINDSIGHT_CLAUDE_BIN;
    else process.env.HINDSIGHT_CLAUDE_BIN = ORIGINAL_ENV;
  });

  it("an explicit argument wins over everything", () => {
    process.env.HINDSIGHT_CLAUDE_BIN = "/env/claude";
    expect(resolveClaudeBin("/explicit/claude")).toBe("/explicit/claude");
  });

  it("HINDSIGHT_CLAUDE_BIN env var wins when no explicit arg is given", () => {
    process.env.HINDSIGHT_CLAUDE_BIN = "/env/claude";
    expect(resolveClaudeBin()).toBe("/env/claude");
  });

  it("falls back to the bare 'claude' PATH lookup when nothing else resolves", () => {
    delete process.env.HINDSIGHT_CLAUDE_BIN;
    // The native-installer path (~/.claude/local/claude) won't exist in CI/test envs, so this
    // exercises the final fallback. (If it DOES exist on a dev machine, that's still a valid,
    // documented resolution — so only assert the env/explicit-less case falls back to *something*.)
    const bin = resolveClaudeBin();
    expect(typeof bin).toBe("string");
    expect(bin.length).toBeGreaterThan(0);
  });
});

describe("startCodebaseSurvey", () => {
  function fakeSpawn() {
    return vi.fn().mockReturnValue({ on: vi.fn(), unref: vi.fn() });
  }

  it("spawns the resolved claude binary with the expected argv and options", () => {
    const spawn = fakeSpawn();
    startCodebaseSurvey("/repo", {
      model: "sonnet",
      mcpServerPath: "/x/mcp-server.js",
      claudeBin: "/bin/claude",
      spawn,
    });

    expect(spawn).toHaveBeenCalledTimes(1);
    const [bin, argv, options] = spawn.mock.calls[0];
    expect(bin).toBe("/bin/claude");

    expect(argv).toContain("-p");
    expect(argv).toContain(SURVEY_PROMPT);
    expect(argv).toContain("--model");
    expect(argv).toContain("sonnet");
    expect(argv).toContain("--mcp-config");
    expect(argv).toContain("--strict-mcp-config");
    expect(argv).toContain("--allowedTools");
    expect(argv).toContain("Read");
    expect(argv).toContain("Glob");
    expect(argv).toContain("Grep");
    expect(argv).toContain("mcp__hindsight__agent_knowledge_ingest");

    // Sandbox: no bypassPermissions (it defeats --allowedTools — empirically verified against the
    // live `claude` binary), and a --disallowedTools deny-list covering every dangerous tool.
    expect(argv).not.toContain("--permission-mode");
    expect(argv).not.toContain("bypassPermissions");
    expect(argv).toContain("--disallowedTools");
    for (const t of ["Bash", "Write", "Edit", "NotebookEdit", "WebFetch", "WebSearch", "Task"]) {
      expect(argv).toContain(t);
    }

    // Spend cap.
    expect(argv).toContain("--max-budget-usd");
    expect(argv).toContain("0.5");

    const mcpConfigIdx = argv.indexOf("--mcp-config");
    const mcpConfigJson = argv[mcpConfigIdx + 1];
    expect(mcpConfigJson).toContain("/x/mcp-server.js");
    expect(mcpConfigJson).toContain("HINDSIGHT_MCP_PROJECT_CWD");
    const parsed = JSON.parse(mcpConfigJson);
    expect(parsed.mcpServers.hindsight.command).toBe("node");
    expect(parsed.mcpServers.hindsight.args).toEqual(["/x/mcp-server.js"]);
    expect(parsed.mcpServers.hindsight.env.HINDSIGHT_MCP_PROJECT_CWD).toBe("/repo");

    expect(options.cwd).toBe("/repo");
    expect(options.detached).toBe(true);
    expect(options.stdio).toBe("ignore");
    expect(options.env.HINDSIGHT_DISABLE_HOOKS).toBe("1");

    const child = spawn.mock.results[0].value;
    expect(child.on).toHaveBeenCalledWith("error", expect.any(Function));
    expect(child.unref).toHaveBeenCalled();
  });

  it("defaults model to 'haiku' when opts.model is omitted", () => {
    const spawn = fakeSpawn();
    startCodebaseSurvey("/repo", {
      mcpServerPath: "/x/mcp-server.js",
      claudeBin: "/bin/claude",
      spawn,
    });
    const argv = spawn.mock.calls[0][1];
    const modelIdx = argv.indexOf("--model");
    expect(argv[modelIdx + 1]).toBe("haiku");
  });

  it("defaults --max-budget-usd to 0.5 when opts.budgetUsd is omitted", () => {
    const spawn = fakeSpawn();
    startCodebaseSurvey("/repo", {
      mcpServerPath: "/x/mcp-server.js",
      claudeBin: "/bin/claude",
      spawn,
    });
    const argv = spawn.mock.calls[0][1];
    const idx = argv.indexOf("--max-budget-usd");
    expect(argv[idx + 1]).toBe("0.5");
  });

  it("passes a custom opts.budgetUsd through as --max-budget-usd", () => {
    const spawn = fakeSpawn();
    startCodebaseSurvey("/repo", {
      mcpServerPath: "/x/mcp-server.js",
      claudeBin: "/bin/claude",
      budgetUsd: 2,
      spawn,
    });
    const argv = spawn.mock.calls[0][1];
    const idx = argv.indexOf("--max-budget-usd");
    expect(argv[idx + 1]).toBe("2");
  });

  it("resolves mcpServerPath as a sibling of this module by default", () => {
    const spawn = fakeSpawn();
    startCodebaseSurvey("/repo", { claudeBin: "/bin/claude", spawn });
    const argv = spawn.mock.calls[0][1];
    const mcpConfigJson = argv[argv.indexOf("--mcp-config") + 1];
    const parsed = JSON.parse(mcpConfigJson);
    expect(parsed.mcpServers.hindsight.args[0]).toContain("mcp-server.js");
  });

  it("fail-safe: a spawn that throws synchronously does not throw out of startCodebaseSurvey", () => {
    const spawn = vi.fn().mockImplementation(() => {
      throw new Error("spawn EMFILE");
    });
    expect(() =>
      startCodebaseSurvey("/repo", {
        mcpServerPath: "/x/mcp-server.js",
        claudeBin: "/bin/claude",
        spawn,
      })
    ).not.toThrow();
  });

  it("fail-safe: an async 'error' event on the child does not crash the caller", async () => {
    const { EventEmitter } = await import("node:events");
    const child = new EventEmitter() as InstanceType<typeof EventEmitter> & { unref: () => void };
    child.unref = vi.fn();
    const spawn = vi.fn().mockReturnValue(child);
    expect(() =>
      startCodebaseSurvey("/repo", {
        mcpServerPath: "/x/mcp-server.js",
        claudeBin: "/bin/claude",
        spawn,
      })
    ).not.toThrow();
    expect(() => child.emit("error", new Error("ENOENT"))).not.toThrow();
  });
});
