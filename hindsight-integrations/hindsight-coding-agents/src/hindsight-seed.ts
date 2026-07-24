#!/usr/bin/env node
/** hindsight-seed — control command the agent runs after the seed offer: `seed`/`decline` a repo's bank. */
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
