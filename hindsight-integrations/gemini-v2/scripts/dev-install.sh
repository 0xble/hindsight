#!/usr/bin/env bash
# Dev install for the Gemini CLI wrapper. Modifying ~/.gemini/settings.json affects your live Gemini
# sessions.
#
# Builds the wrapper bundle, then MERGES the three hooks (SessionStart + BeforeAgent + SessionEnd)
# and the Hindsight MCP server into ~/.gemini/settings.json — backing up the existing file first.
# Gemini keeps hooks + mcpServers in one settings.json (which usually already holds your auth), so we
# merge into it rather than overwrite. Requires Gemini CLI >= 0.52.0 (older versions have no hooks).
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WRAPPER_DIR="$(cd "${HERE}/.." && pwd)"
DIST="${WRAPPER_DIR}/dist"
GEMINI_DIR="${HOME}/.gemini"
SETTINGS="${GEMINI_DIR}/settings.json"

echo "[dev-install] building wrapper bundle …"
node "${WRAPPER_DIR}/scripts/build.mjs"

mkdir -p "${GEMINI_DIR}"
if [ -f "${SETTINGS}" ]; then
  cp "${SETTINGS}" "${SETTINGS}.bak"
  echo "[dev-install] backed up existing settings.json -> settings.json.bak"
fi

# Merge hooks + mcpServers into settings.json (preserving every other key) via Node — safe JSON,
# no fragile text munging. BeforeAgent = Gemini's per-turn (recall) event; SessionEnd = write-back.
DIST="${DIST}" SETTINGS="${SETTINGS}" node <<'NODE'
import { readFileSync, writeFileSync, existsSync } from "node:fs";

const dist = process.env.DIST;
const path = process.env.SETTINGS;
const settings = existsSync(path) ? JSON.parse(readFileSync(path, "utf8")) : {};

const cmd = (file, timeout) => ({
  hooks: [{ type: "command", command: `node "${dist}/${file}"`, timeout }],
});

settings.hooks = {
  ...(settings.hooks ?? {}),
  SessionStart: [cmd("gemini-sessionstart-hook.js", 15000)],
  BeforeAgent: [cmd("gemini-hook.js", 30000)],
  SessionEnd: [cmd("gemini-stop-hook.js", 30000)],
};

settings.mcpServers = {
  ...(settings.mcpServers ?? {}),
  hindsight: {
    command: "node",
    args: [`${dist}/mcp-server.js`],
    env: { HINDSIGHT_MCP_HARNESS: "gemini" },
  },
};

writeFileSync(path, JSON.stringify(settings, null, 2) + "\n");
console.log(`[dev-install] merged hooks (SessionStart/BeforeAgent/SessionEnd) + mcpServers.hindsight into ${path}`);
NODE

echo ""
echo "=================================================================="
echo " Done. Restart Gemini CLI to load the hooks + MCP server."
echo ""
echo " Full integration: SessionStart seed + per-turn recall (BeforeAgent)"
echo " + SessionEnd write-back (sessions compound into memory) + MCP tools."
echo ""
echo " Requires Gemini CLI >= 0.52.0. Your previous settings.json was"
echo " backed up to settings.json.bak."
echo "=================================================================="
