import { execSync } from "node:child_process";
import { cpSync, mkdirSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const wrapper = join(here, "..");
const core = join(wrapper, "..", "hindsight-coding-agents");

console.log("[build] building core bundle (tsup) …");
execSync("npm run build", { cwd: core, stdio: "inherit" });

mkdirSync(join(wrapper, "dist"), { recursive: true });
for (const f of ["claude-hook.js"]) {
  cpSync(join(core, "dist", f), join(wrapper, "dist", f));
  console.log(`[build] copied ${f} -> claude-code-v2/dist/${f}`);
}
// Node needs an unambiguous module type next to the bundle (the core's own
// package.json sets "type": "module", but we only copy the one file, not the
// whole package) — otherwise every run reparses and warns on stderr.
writeFileSync(join(wrapper, "dist", "package.json"), JSON.stringify({ type: "module" }, null, 2) + "\n");
console.log("[build] done");
