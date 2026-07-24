#!/usr/bin/env node
/**
 * hindsight-seed — control command the agent runs after the seed offer: `seed`/`decline` a repo's bank.
 *
 * Claude-Code-only for now: the harness is hardcoded to "claude-code" below, since this CLI is only
 * ever invoked from the Claude Code SessionStart flow (Task 10b). If it's ever driven from another
 * harness's context — especially one with a non-default `bankIdTemplate` or a `harnesses.<name>`
 * override in the config — the hardcoded harness would resolve the wrong bank. Generalizing would
 * need a real `--harness` flag, not added here.
 */
import { loadConfig } from "./core/config";
import { deriveBankId } from "./core/bank";
import { seedControl } from "./core/seed";

function arg(name: string): string | undefined {
  const i = process.argv.indexOf(`--${name}`);
  return i >= 0 && i + 1 < process.argv.length ? process.argv[i + 1] : undefined;
}

const command = process.argv[2] || "";
const repo = arg("repo") || process.cwd();
const cfg = loadConfig({ harness: "claude-code", projectDir: repo });
const bankId = deriveBankId(cfg, repo, "claude-code");
const result = seedControl(command, { repo, bankId });
console.log(result.message);
process.exit(result.ok ? 0 : 1);
