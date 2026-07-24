#!/usr/bin/env node
/** hindsight-claude-stop-hook — Claude Code `Stop` hook: writes the session transcript back to memory. */
import { runRetainHook } from "./core/retain-hook";

void runRetainHook({
  harness: "claude-code",
  parse: (ev) => ({
    sessionId: ev.session_id as string | undefined,
    transcriptPath: ev.transcript_path as string | undefined,
    cwd: ev.cwd as string | undefined,
  }),
});
