import { mkdtempSync, mkdirSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { loadConfig } from "./config";

let root: string;
let globalCfg: string;

function writeJson(path: string, value: unknown): void {
  mkdirSync(join(path, ".."), { recursive: true });
  writeFileSync(path, JSON.stringify(value));
}

beforeEach(() => {
  root = mkdtempSync(join(tmpdir(), "hs-cfg-"));
  globalCfg = join(root, "global.json");
});

afterEach(() => {
  rmSync(root, { recursive: true, force: true });
});

describe("loadConfig layering", () => {
  it("missing files yield defaults", () => {
    const cfg = loadConfig({ path: join(root, "nope.json") });
    expect(cfg.apiUrl).toBe("https://api.hindsight.vectorize.io");
    expect(cfg.bankId).toBeUndefined();
    expect(cfg.disabled).toBe(false);
  });

  it("malformed global file falls back to defaults with a warning", () => {
    writeFileSync(globalCfg, "{not json");
    const err = vi.spyOn(console, "error").mockImplementation(() => {});
    const cfg = loadConfig({ path: globalCfg });
    expect(cfg.apiUrl).toBe("https://api.hindsight.vectorize.io");
    expect(err).toHaveBeenCalledOnce();
    err.mockRestore();
  });

  it("applies the requesting harness's section over the base", () => {
    writeJson(globalCfg, {
      apiUrl: "http://x:1",
      bankId: "shared",
      harnesses: {
        "claude-code": { bankId: "claude-bank" },
        opencode: { disabled: true },
      },
    });
    expect(loadConfig({ path: globalCfg, harness: "claude-code" }).bankId).toBe("claude-bank");
    expect(loadConfig({ path: globalCfg, harness: "claude-code" }).apiUrl).toBe("http://x:1");
    expect(loadConfig({ path: globalCfg, harness: "opencode" }).disabled).toBe(true);
    expect(loadConfig({ path: globalCfg, harness: "opencode" }).bankId).toBe("shared");
    expect(loadConfig({ path: globalCfg }).bankId).toBe("shared"); // no harness: base only
  });





  it("legacy string signature still works as the global path", () => {
    writeJson(globalCfg, { bankId: "legacy" });
    expect(loadConfig(globalCfg).bankId).toBe("legacy");
  });

  it("pageRefreshEveryTurns defaults to 10", () => {
    expect(
      loadConfig({ harness: "claude-code"}).pageRefreshEveryTurns
    ).toBe(10);
  });

  it("pageRefreshEveryTurns override wins over the default", () => {
    writeJson(globalCfg, { pageRefreshEveryTurns: 25 });
    expect(loadConfig({ path: globalCfg }).pageRefreshEveryTurns).toBe(25);
  });
});

// A project-local .hindsight/coding-agent.json comes from the (untrusted) opened repo. It must not be
// able to redirect the API endpoint/token or the global bank map — otherwise a malicious repo could
// exfiltrate the user's token + prompts to its own server just by being opened.
describe("loadConfig — untrusted project-local layer is sanitized (security)", () => {



  it("the user-global config CAN still set apiUrl/apiToken (only the project layer is restricted)", () => {
    writeJson(globalCfg, { apiUrl: "https://real.example", apiToken: "REAL-TOKEN" });
    const cfg = loadConfig({ path: globalCfg });
    expect(cfg.apiUrl).toBe("https://real.example");
    expect(cfg.apiToken).toBe("REAL-TOKEN");
  });
});
