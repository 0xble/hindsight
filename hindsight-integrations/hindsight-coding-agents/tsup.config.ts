import { defineConfig } from "tsup";

export default defineConfig({
  entry: {
    index: "src/index.ts",
    backfill: "src/backfill.ts",
    "claude-hook": "src/claude-hook.ts",
    "claude-stop-hook": "src/claude-stop-hook.ts",
    "cursor-hook": "src/cursor-hook.ts",
    "codex-hook": "src/codex-hook.ts",
  },
  format: ["esm"],
  target: "node18",
  clean: true,
  dts: { entry: "src/index.ts" },
  shims: false,
  // Each bin entry (claude-hook.js, cursor-hook.js, codex-hook.js, backfill.js) must be a
  // single self-contained file: plugin wrappers (e.g. claude-code-v2/scripts/build.mjs) copy
  // just that one file out of dist/, so shared code can't live in a separate chunk-*.js.
  splitting: false,
});
