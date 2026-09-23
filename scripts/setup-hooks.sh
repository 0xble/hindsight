#!/bin/bash
# Optional hooks scoped to the selected worktree, including linked checkouts.
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"
if [ "$(git config --local --get extensions.worktreeConfig || true)" != true ]; then
    # These exceptional shared settings need an explicit Git-config migration.
    if git config --local --get core.worktree >/dev/null ||
       [ "$(git config --local --get core.bare || true)" = true ]; then
        echo 'Cannot enable worktree hooks until core.worktree/core.bare configuration is migrated.' >&2
        exit 1
    fi
    git config --local extensions.worktreeConfig true
fi
git config --worktree core.hooksPath .githooks
echo 'Optional formatting hooks enabled for this worktree.'
