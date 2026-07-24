#!/usr/bin/env bash
# Dev install. Modifying ~/.claude affects your live Claude Code session — run intentionally.
#
# Builds the wrapper (via scripts/build.mjs) and copies it into the local plugin
# cache so it can be picked up as an installed Claude Code plugin. This script is
# SAFE: it never edits ~/.claude/plugins/installed_plugins.json or
# ~/.claude/settings.json — it only builds, copies files, and prints the exact
# JSON you need to add yourself, then you restart Claude Code.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WRAPPER_DIR="$(cd "${HERE}/.." && pwd)"
PLUGIN_NAME="hindsight-memory-v2"
PLUGIN_VERSION="0.1.0"
MARKETPLACE="hindsight"
INSTALL_DIR="${HOME}/.claude/plugins/cache/${MARKETPLACE}/${PLUGIN_NAME}/${PLUGIN_VERSION}"

echo "[dev-install] building wrapper bundle …"
node "${WRAPPER_DIR}/scripts/build.mjs"

echo "[dev-install] copying wrapper into ${INSTALL_DIR} …"
mkdir -p "${INSTALL_DIR}"
cp -R "${WRAPPER_DIR}/.claude-plugin" "${INSTALL_DIR}/"
cp -R "${WRAPPER_DIR}/hooks" "${INSTALL_DIR}/"
cp -R "${WRAPPER_DIR}/dist" "${INSTALL_DIR}/"

echo ""
echo "=================================================================="
echo " Copy complete. Claude Code will NOT pick this up automatically —"
echo " you must register + enable it yourself, and disable the old"
echo " 'hindsight-memory' plugin first to avoid double-injecting memory"
echo " context on every prompt."
echo "=================================================================="
echo ""
echo "1) DISABLE the existing plugin first — in ~/.claude/settings.json"
echo "   set its enabledPlugins entry to false:"
echo ""
echo '   "enabledPlugins": {'
echo '     "hindsight-memory@hindsight": false'
echo '   }'
echo ""
echo "2) Register this plugin — add an entry like this to the \"plugins\""
echo "   object in ~/.claude/plugins/installed_plugins.json:"
echo ""
cat <<JSON
   "${PLUGIN_NAME}@${MARKETPLACE}": [
     {
       "scope": "user",
       "installPath": "${INSTALL_DIR}",
       "version": "${PLUGIN_VERSION}",
       "installedAt": "$(date -u +%Y-%m-%dT%H:%M:%S.000Z)",
       "lastUpdated": "$(date -u +%Y-%m-%dT%H:%M:%S.000Z)"
     }
   ]
JSON
echo ""
echo "3) Enable it — add this to the \"enabledPlugins\" object in"
echo "   ~/.claude/settings.json:"
echo ""
echo '   "enabledPlugins": {'
echo "     \"${PLUGIN_NAME}@${MARKETPLACE}\": true"
echo '   }'
echo ""
echo "4) Restart Claude Code for the new plugin + hook registration to take"
echo "   effect."
echo ""
echo "This script did NOT modify installed_plugins.json or settings.json —"
echo "apply the snippets above by hand."
echo "=================================================================="
