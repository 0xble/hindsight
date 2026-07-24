/**
 * Shared runtime for HOOK-based harnesses (Claude Code, Cursor CLI, ...).
 *
 * Persistent-plugin harnesses (opencode) get a long-lived RuntimeCore; hook harnesses invoke a
 * fresh process per prompt. Every prompt runs `recall` and injects a `<hindsight_memories>` block.
 * On top of that, the session's FIRST prompt also runs `reflect` and injects its answer; the
 * outcome (even an empty one) is cached in tmp so later prompts in the same session recall only
 * and don't repeat reflect. A failed reflect never breaks the agent, never blocks recall, and is
 * always recorded in the diagnostic file, so a silently memory-less session can't masquerade as a
 * memory session.
 *
 * A harness plugs in with a HookSpec: its name, how to read (prompt, cwd, sessionId) from its
 * stdin event, and how to wrap injected context in its native output schema. The pure logic lives
 * in `buildHookOutput` (client + cache file in, injection string out) so it's unit-testable
 * without stdin/stdout; `runHook` is thin plumbing around it, with a `makeClient` seam for tests.
 */
import { appendFileSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { deriveBankId } from "./bank";
import type { Config } from "./config";
import { loadConfig } from "./config";
import type { ClientOpts, RecallResult } from "./hindsight";
import { HindsightClient } from "./hindsight";
import { buildSystemInjection } from "./inject";
import { formatMemories } from "./recall";

export interface HookEventFields {
  prompt?: string;
  cwd?: string;
  sessionId?: string;
}

export interface HookSpec {
  /** Harness name — config `harnesses.<name>` section, {harness} template field, diag records. */
  harness: string;
  /** Read the fields out of the harness's stdin event (shapes differ per harness). */
  parse(event: Record<string, unknown>): HookEventFields;
  /** Wrap injected context in the harness's native hook-output schema. */
  emit(context: string): unknown;
}

export function diag(harness: string, event: string, extra: Record<string, unknown> = {}): void {
  try {
    appendFileSync(
      process.env.HINDSIGHT_DIAG_FILE || "/tmp/hindsight-plugin.log",
      JSON.stringify({ ts: new Date().toISOString(), harness, event, ...extra }) + "\n"
    );
  } catch {
    /* diagnostics must not break the agent */
  }
}

/** Minimal client shape `buildHookOutput` needs — `HindsightClient` satisfies it structurally. */
interface HookClient {
  reflect(query: string, opts: { budget?: string; timeoutMs?: number }): Promise<string>;
  recall(query: string, opts: { maxTokens?: number; timeoutMs?: number }): Promise<RecallResult[]>;
}

/**
 * Pure hook logic: recall every turn; reflect (and cache the outcome) only on the session's first
 * turn. Returns the combined injection string, or `undefined` when there's nothing to inject.
 */
export async function buildHookOutput(args: {
  harness: string;
  prompt: string;
  cfg: Config;
  client: HookClient;
  cacheFile: string;
}): Promise<string | undefined> {
  const { harness, prompt, cfg, client, cacheFile } = args;

  let firstTurn = true;
  try {
    const cached = JSON.parse(readFileSync(cacheFile, "utf8")) as { answer?: string };
    if (typeof cached.answer === "string") firstTurn = false; // reflect already ran this session
  } catch {
    /* missing/invalid cache — first prompt of the session */
  }

  // Kick off recall immediately so it runs concurrently with reflect on the first turn.
  const tRecall = Date.now();
  const recallP = client.recall(prompt, {
    maxTokens: cfg.recallMaxTokens,
    timeoutMs: cfg.recallTimeoutMs,
  });

  let reflectBlock = "";
  if (firstTurn) {
    const t0 = Date.now();
    let answer = "";
    try {
      answer = await client.reflect(prompt, { budget: "high", timeoutMs: cfg.reflectTimeoutMs });
      diag(harness, answer ? "reflect_ok" : "reflect_empty", {
        ms: Date.now() - t0,
        chars: answer.length,
        query: prompt.slice(0, 80),
      });
    } catch (e) {
      diag(harness, "reflect_failed", {
        ms: Date.now() - t0,
        error: String((e as Error)?.message || e).slice(0, 200),
        query: prompt.slice(0, 80),
      });
    }
    try {
      mkdirSync(dirname(cacheFile), { recursive: true });
      writeFileSync(cacheFile, JSON.stringify({ answer }));
    } catch {
      /* cache is best-effort; worst case we reflect again next prompt */
    }
    reflectBlock = answer ? buildSystemInjection(answer) : "";
  }

  let results: RecallResult[] = [];
  try {
    results = await recallP;
    diag(harness, results.length ? "recall_ok" : "recall_empty", {
      ms: Date.now() - tRecall,
      count: results.length,
      query: prompt.slice(0, 80),
    });
  } catch (e) {
    diag(harness, "recall_failed", {
      ms: Date.now() - tRecall,
      error: String((e as Error)?.message || e).slice(0, 200),
      query: prompt.slice(0, 80),
    });
  }
  const memBlock = formatMemories(results);

  const blocks = [reflectBlock, memBlock].filter(Boolean);
  return blocks.length ? blocks.join("\n\n") : undefined;
}

/** Run one hook invocation: stdin event in, (maybe) an injection object on stdout. */
export async function runHook(
  spec: HookSpec,
  makeClient: (opts: ClientOpts) => HookClient = (o) => new HindsightClient(o)
): Promise<void> {
  let ev: Record<string, unknown> = {};
  try {
    ev = JSON.parse(readFileSync(0, "utf8")) as Record<string, unknown>;
  } catch {
    return; // no/invalid event: stay silent
  }
  const { prompt: rawPrompt, cwd, sessionId } = spec.parse(ev);
  const prompt = (rawPrompt || "").trim();
  if (!prompt) return;

  const cfg = loadConfig({ harness: spec.harness, projectDir: cwd });
  if (cfg.disabled) return;

  const out = (context: string) => process.stdout.write(JSON.stringify(spec.emit(context)));

  const client = makeClient({
    apiUrl: cfg.apiUrl,
    apiToken: cfg.apiToken,
    bank: deriveBankId(cfg, cwd || process.cwd(), spec.harness),
  });
  const cacheFile = join(
    tmpdir(),
    `hindsight-${spec.harness}`,
    `${sessionId || "no-session"}.json`
  );

  const output = await buildHookOutput({ harness: spec.harness, prompt, cfg, client, cacheFile });
  if (output) out(output);
}
