#!/usr/bin/env bash
# Dev install for the Codex CLI wrapper. Modifying ~/.codex affects your live Codex sessions.
#
# Builds the wrapper and writes ~/.codex/hooks.json pointing at the bundled node hooks (backing up
# any existing hooks.json first). It does NOT edit ~/.codex/config.toml automatically (TOML is
# risky to merge by script) — it prints the exact lines to add. Then restart Codex.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WRAPPER_DIR="$(cd "${HERE}/.." && pwd)"
DIST="${WRAPPER_DIR}/dist"
CODEX_DIR="${HOME}/.codex"
HOOKS_JSON="${CODEX_DIR}/hooks.json"

echo "[dev-install] building wrapper bundle …"
node "${WRAPPER_DIR}/scripts/build.mjs"

mkdir -p "${CODEX_DIR}"
if [ -f "${HOOKS_JSON}" ]; then
  cp "${HOOKS_JSON}" "${HOOKS_JSON}.bak"
  echo "[dev-install] backed up existing hooks.json -> hooks.json.bak"
fi

# SessionStart (seed) + UserPromptSubmit (recall) + Stop (write-back).
cat > "${HOOKS_JSON}" <<JSON
{
  "hooks": {
    "SessionStart": [
      { "hooks": [ { "type": "command", "command": "node \"${DIST}/codex-sessionstart-hook.js\"", "timeout": 15 } ] }
    ],
    "UserPromptSubmit": [
      { "hooks": [ { "type": "command", "command": "node \"${DIST}/codex-hook.js\"", "timeout": 30 } ] }
    ],
    "Stop": [
      { "hooks": [ { "type": "command", "command": "node \"${DIST}/codex-stop-hook.js\"", "timeout": 30 } ] }
    ]
  }
}
JSON
echo "[dev-install] wrote ${HOOKS_JSON}"

echo ""
echo "=================================================================="
echo " Wrote ~/.codex/hooks.json. Two things to do by hand in"
echo " ~/.codex/config.toml, then restart Codex:"
echo "=================================================================="
echo ""
echo "1) Enable hooks (Codex CLI >= 0.116):"
echo ""
echo "   [features]"
echo "   codex_hooks = true"
echo ""
echo "2) Register the Hindsight MCP server (knowledge-page + recall tools):"
echo ""
echo "   [mcp_servers.hindsight]"
echo "   command = \"node\""
echo "   args = [\"${DIST}/mcp-server.js\"]"
echo "   env = { HINDSIGHT_MCP_HARNESS = \"codex\" }"
echo ""
echo " If you previously installed the Python Codex integration, its"
echo " hooks.json was backed up to hooks.json.bak (this one replaces it so"
echo " memory isn't injected twice)."
echo ""
echo " Full integration: SessionStart seed + per-turn recall + Stop"
echo " write-back (sessions compound into memory) + MCP tools."
echo "=================================================================="
