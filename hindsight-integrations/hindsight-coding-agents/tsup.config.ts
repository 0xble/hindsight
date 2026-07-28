import { defineConfig } from "tsup";

export default defineConfig({
  entry: {
    index: "src/index.ts",
    deepen: "src/deepen.ts",
    installer: "src/installer.ts",
    status: "src/status.ts",
    "claude-hook": "src/claude-hook.ts",
    "claude-stop-hook": "src/claude-stop-hook.ts",
    "claude-sessionstart-hook": "src/claude-sessionstart-hook.ts",
    "cursor-hook": "src/cursor-hook.ts",
    "codex-hook": "src/codex-hook.ts",
    "codex-sessionstart-hook": "src/codex-sessionstart-hook.ts",
    "codex-stop-hook": "src/codex-stop-hook.ts",
    "gemini-hook": "src/gemini-hook.ts",
    "gemini-sessionstart-hook": "src/gemini-sessionstart-hook.ts",
    "gemini-stop-hook": "src/gemini-stop-hook.ts",
    "mcp-server": "src/mcp-server.ts",
    "hindsight-seed": "src/hindsight-seed.ts",
  },
  format: ["esm"],
  target: "node18",
  clean: true,
  dts: { entry: "src/index.ts" },
  shims: false,
  // Each bin entry (claude-hook.js, cursor-hook.js, codex-hook.js, deepen.js, status.js, mcp-server.js,
  // hindsight-seed.js) must be a single self-contained file: plugin wrappers (e.g.
  // claude-code-v2/scripts/build.mjs) copy just that one file out of dist/, so shared code can't
  // live in a separate chunk-*.js.
  splitting: false,
  // mcp-server.js additionally needs its npm deps (the MCP SDK + zod) inlined, since the wrapper
  // only copies the single bundle file, not node_modules. The regexes catch subpath imports too
  // (e.g. "@modelcontextprotocol/sdk/server/mcp.js", "zod/v4/...").
  noExternal: [/^@modelcontextprotocol\/sdk/, /^zod/],
});
