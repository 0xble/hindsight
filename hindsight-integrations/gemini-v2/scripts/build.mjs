import { execSync } from "node:child_process";
import { cpSync, mkdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { builtinModules } from "node:module";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const wrapper = join(here, "..");
const core = join(wrapper, "..", "hindsight-coding-agents");
const distDir = join(wrapper, "dist");

console.log("[build] building core bundle (tsup) …");
execSync("npm run build", { cwd: core, stdio: "inherit" });

// Clean dist/ so a stale leftover can't get shipped.
rmSync(distDir, { recursive: true, force: true });
mkdirSync(distDir, { recursive: true });

// Full Gemini integration: SessionStart (seed) + BeforeAgent (recall) + SessionEnd (write-back) + MCP.
const bundleFiles = [
  "gemini-hook.js",
  "gemini-sessionstart-hook.js",
  "gemini-stop-hook.js",
  "mcp-server.js",
  "hindsight-seed.js",
  "deepen.js",
];
for (const f of bundleFiles) {
  cpSync(join(core, "dist", f), join(distDir, f));
  console.log(`[build] copied ${f} -> gemini-v2/dist/${f}`);
}
writeFileSync(join(distDir, "package.json"), JSON.stringify({ type: "module" }, null, 2) + "\n");

// Self-contained-bundle guard: each copied file must import only Node builtins (mcp-server inlines
// its npm deps via tsup noExternal). Same check as the Claude/Codex wrappers.
const allowed = new Set([...builtinModules, ...builtinModules.map((m) => `node:${m}`)]);
for (const f of bundleFiles) {
  const bundled = readFileSync(join(distDir, f), "utf8");
  const importSpecifiers = new Set();
  for (const m of bundled.matchAll(/^\s*import\b[^'"]*['"]([^'"]+)['"]/gm)) {
    importSpecifiers.add(m[1]);
  }
  const offenders = [...importSpecifiers].filter((spec) => !allowed.has(spec));
  if (offenders.length > 0) {
    console.error(
      `[build] ERROR: dist/${f} is not self-contained. Non-builtin import(s): ${offenders.join(", ")}`
    );
    process.exit(1);
  }
  console.log(`[build] guard: dist/${f} imports only Node builtins — self-contained ✓`);
}
console.log("[build] done");
