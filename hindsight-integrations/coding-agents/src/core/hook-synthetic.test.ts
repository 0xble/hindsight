import { existsSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { basename, join } from "node:path";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { loadConfig, resolveConfig } from "./config";
import { buildHookOutput, runHook } from "./hook";
import { ReflectError } from "./hindsight";
import { buildReflectQuery } from "./inject";
import { sessionCacheFile } from "./session-cache";
import { HOOK_HARNESSES } from "../harness/hook-lifecycle";

let stdin = "";
vi.mock("node:fs", async (importOriginal) => {
  const actual = await importOriginal<typeof import("node:fs")>();
  return {
    ...actual,
    readFileSync: (target: unknown, ...rest: unknown[]) =>
      target === 0 ? stdin : (actual.readFileSync as (...a: unknown[]) => unknown)(target, ...rest),
  };
});

vi.mock("./config", async (importOriginal) => {
  const actual = await importOriginal<typeof import("./config")>();
  return {
    ...actual,
    loadConfig: vi.fn(() =>
      actual.resolveConfig({ bankId: "hook-synthetic-test", autoSeed: false, autoUpdate: false })
    ),
  };
});

let root: string;
let cacheFile: string;
let nativeCache: string;

beforeEach(() => {
  root = mkdtempSync(join(tmpdir(), "hs-hook-synthetic-"));
  cacheFile = join(root, "cache.json");
  nativeCache = sessionCacheFile("claude-code", basename(root));
  vi.mocked(loadConfig).mockClear();
  vi.stubEnv("HINDSIGHT_DIAG_FILE", join(root, "diag.jsonl"));
  vi.stubEnv("HINDSIGHT_LOG_FILE", join(root, "plugin.log"));
});

afterEach(() => {
  vi.unstubAllEnvs();
  rmSync(root, { recursive: true, force: true });
  rmSync(nativeCache, { force: true });
  rmSync(nativeCache.replace(/\.json$/, ".root"), { force: true });
});

const makeClient = () => ({
  reflect: vi.fn(async () => "A past decision."),
  listPages: vi.fn(async () => ({ items: [] })),
  searchKnowledgePages: vi.fn(async () => []),
  recallObservations: vi.fn(async () => [] as string[]),
});

const notification = "<task-notification><status>completed</status>transport</task-notification>";
const reminder = "<system-reminder>injected guidance</system-reminder>";
const prompt = "Why did we choose bounded retries?";

describe("automatic memory injection excludes synthetic hook prompts", () => {
  it.each(["reflect", "pages", "recall", "none"] as const)(
    "%s leaves wholly synthetic and empty prompts unresolved without network work",
    async (autoInject) => {
      const client = makeClient();
      for (const synthetic of [notification, reminder, `${notification}\n${reminder}`, " \n "]) {
        const out = await buildHookOutput({
          harness: "claude-code",
          prompt: synthetic,
          cfg: resolveConfig({ autoInject }),
          client,
          cacheFile,
        });
        expect(out).toEqual({ pages: [] });
        expect(existsSync(cacheFile)).toBe(false);
      }
      expect(client.reflect).not.toHaveBeenCalled();
      expect(client.searchKnowledgePages).not.toHaveBeenCalled();
      expect(client.recallObservations).not.toHaveBeenCalled();
      expect(client.listPages).not.toHaveBeenCalled();
    }
  );

  it("keeps an existing session's deferred first prompt and page cadence unchanged", async () => {
    const previous = JSON.stringify({
      turns: 3,
      reflectAttempts: 1,
      deferInitialReflect: true,
      pages: { atTurn: 1, list: [{ id: "p1", title: "Decisions" }] },
    });
    writeFileSync(cacheFile, previous);
    const client = makeClient();
    const cfg = resolveConfig({});
    const args = { harness: "claude-code", cfg, client, cacheFile };
    const out = await buildHookOutput({ ...args, prompt: notification });
    expect(out).toEqual({ pages: [{ id: "p1", title: "Decisions" }] });
    expect(readFileSync(cacheFile, "utf8")).toBe(previous);
    expect(client.listPages).not.toHaveBeenCalled();

    await buildHookOutput({ ...args, prompt });
    expect(client.reflect).not.toHaveBeenCalled();
    expect(JSON.parse(readFileSync(cacheFile, "utf8")).reflectAttempts).toBe(1);
    const next = await buildHookOutput({ ...args, prompt });
    expect(client.reflect).toHaveBeenCalledTimes(1);
    expect(next.context).toContain("A past decision.");
  });

  it.each(["reflect", "pages", "recall"] as const)(
    "%s queries only the real text around injected wrappers",
    async (autoInject) => {
      const client = makeClient();
      await buildHookOutput({
        harness: "claude-code",
        prompt: `${notification}\n${prompt}\n<hindsight_memory>old answer</hindsight_memory>`,
        cfg: resolveConfig({ autoInject }),
        client,
        cacheFile,
      });
      const source =
        autoInject === "reflect"
          ? client.reflect
          : autoInject === "pages"
            ? client.searchKnowledgePages
            : client.recallObservations;
      expect(source).toHaveBeenCalledWith(
        autoInject === "reflect" ? buildReflectQuery(prompt) : prompt,
        expect.any(Object)
      );
      expect(readFileSync(join(root, "diag.jsonl"), "utf8")).not.toContain("transport");
    }
  );

  it("retries after reflection and retrieval all fail, even with synthetic turns between", async () => {
    const client = makeClient();
    client.reflect.mockRejectedValueOnce(new ReflectError("reflect 502", 502, false));
    client.searchKnowledgePages.mockRejectedValueOnce(new Error("pages unavailable"));
    client.recallObservations.mockRejectedValueOnce(new Error("recall unavailable"));
    const args = { harness: "claude-code", cfg: resolveConfig({}), client, cacheFile };
    const first = await buildHookOutput({ ...args, prompt });
    expect(first.notice).toContain("no memory this turn");
    expect(JSON.parse(readFileSync(cacheFile, "utf8")).reflectAnswer).toBeUndefined();
    await buildHookOutput({ ...args, prompt: notification });
    expect(JSON.parse(readFileSync(cacheFile, "utf8")).reflectAttempts).toBe(1);
    const next = await buildHookOutput({ ...args, prompt });
    expect(client.reflect).toHaveBeenCalledTimes(2);
    expect(next.context).toContain("A past decision.");
    expect(next.notice).not.toContain("no memory");
  });

  it("does not claim a memory-less session when retrieval recovers reflection", async () => {
    const client = makeClient();
    client.reflect.mockRejectedValueOnce(new ReflectError("reflect 502", 502, false));
    client.recallObservations.mockResolvedValueOnce(["A fallback decision."]);
    const out = await buildHookOutput({
      harness: "claude-code",
      prompt,
      cfg: resolveConfig({}),
      client,
      cacheFile,
    });
    expect(out.context).toContain("A fallback decision.");
    expect(out.notice).toBeUndefined();
    const logged = readFileSync(join(root, "plugin.log"), "utf8");
    expect(logged).toContain("reflect failed");
    expect(logged).not.toContain("without memory");
  });

  it("the native Claude prompt hook skips synthetic events before config or client setup", async () => {
    stdin = JSON.stringify({ prompt: notification, cwd: root, session_id: basename(root) });
    const factory = vi.fn(makeClient);
    const emit = vi.spyOn(process.stdout, "write").mockReturnValue(true);
    try {
      await runHook(HOOK_HARNESSES["claude-code"].prompt, factory);
      expect(loadConfig).not.toHaveBeenCalled();
      expect(factory).not.toHaveBeenCalled();
      expect(emit).not.toHaveBeenCalled();
      expect(existsSync(nativeCache)).toBe(false);

      stdin = JSON.stringify({
        prompt: `${reminder}\n${prompt}`,
        cwd: root,
        session_id: basename(root),
      });
      await runHook(HOOK_HARNESSES["claude-code"].prompt, factory);
      expect(factory).toHaveBeenCalledTimes(1);
      expect(factory.mock.results[0].value.reflect).toHaveBeenCalledWith(
        buildReflectQuery(prompt),
        expect.any(Object)
      );
      expect(emit).toHaveBeenCalledTimes(1);
      expect(JSON.parse(readFileSync(nativeCache, "utf8")).reflectAttempts).toBe(1);
    } finally {
      emit.mockRestore();
    }
  });
});
