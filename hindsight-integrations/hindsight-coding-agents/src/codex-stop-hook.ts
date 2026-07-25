#!/usr/bin/env node
/** hindsight-codex-stop-hook — Codex CLI `Stop` hook: writes the session's rollout transcript back
 *  to memory. Same runtime as the Claude Stop hook, but with the Codex rollout reader. */
import { runRetainHook } from "./core/retain-hook";
import { readCodexTranscript } from "./core/transcript-codex";

void runRetainHook({
  harness: "codex",
  parse: (ev) => ({
    sessionId: ev.session_id as string | undefined,
    transcriptPath: ev.transcript_path as string | undefined,
    cwd: ev.cwd as string | undefined,
  }),
  readTranscript: readCodexTranscript,
});
