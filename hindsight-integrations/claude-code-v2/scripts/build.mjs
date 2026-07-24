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

// Start from a clean dist/ so a file removed from the copy list (or a stale
// leftover from a previous build) can't linger and get shipped by accident.
rmSync(distDir, { recursive: true, force: true });
mkdirSync(distDir, { recursive: true });
for (const f of ["claude-hook.js"]) {
  cpSync(join(core, "dist", f), join(distDir, f));
  console.log(`[build] copied ${f} -> claude-code-v2/dist/${f}`);
}
// Node needs an unambiguous module type next to the bundle (the core's own
// package.json sets "type": "module", but we only copy the one file, not the
// whole package) — otherwise every run reparses and warns on stderr.
writeFileSync(join(distDir, "package.json"), JSON.stringify({ type: "module" }, null, 2) + "\n");

// --- Self-contained-bundle guard -------------------------------------------
// This wrapper copies only the single claude-hook.js file out of the core's
// dist/ (not the whole package), so that file must import nothing but Node
// builtins. If tsup code-splitting ever gets re-enabled (shared code moves to
// a sibling "./chunk-*.js") or claude-hook.ts picks up a real npm dependency,
// the copied file would reference something we didn't bring along and would
// only fail at runtime, in a live Claude Code session. Catch it here instead.
const bundled = readFileSync(join(distDir, "claude-hook.js"), "utf8");
const importSpecifiers = new Set();
// Matches both `import ... from "spec"` and bare `import "spec"` forms.
for (const m of bundled.matchAll(/^\s*import\b[^'"]*['"]([^'"]+)['"]/gm)) {
  importSpecifiers.add(m[1]);
}
const allowed = new Set([...builtinModules, ...builtinModules.map((m) => `node:${m}`)]);
const offenders = [...importSpecifiers].filter((spec) => !allowed.has(spec));
if (offenders.length > 0) {
  console.error(
    `[build] ERROR: dist/claude-hook.js is not self-contained.\n` +
      `  Found import specifier(s) that aren't Node builtins: ${offenders.join(", ")}\n` +
      `  This usually means either:\n` +
      `    - tsup code-splitting got re-enabled in hindsight-coding-agents/tsup.config.ts\n` +
      `      (shared code moved to a "./chunk-*.js" this wrapper doesn't copy), or\n` +
      `    - claude-hook.ts (or something it imports) now depends on an npm package.\n` +
      `  Fix the source so the bundle only imports Node builtins, then rebuild.`
  );
  process.exit(1);
}
console.log("[build] guard: bundle imports only Node builtins — self-contained ✓");
console.log("[build] done");
