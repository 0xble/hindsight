#!/usr/bin/env node
/** hindsight-gemini-stop-hook — Gemini CLI `SessionEnd` hook: writes the session's transcript back
 *  to memory. Same runtime as the Claude/Codex Stop hooks, but with the Gemini JSONL reader. Gemini's
 *  SessionEnd stdin carries only `reason`, but every hook also gets `transcript_path` — the file the
 *  reader parses. */
import { runRetainHook } from "./core/retain-hook";
import { readGeminiTranscript } from "./core/transcript-gemini";

void runRetainHook({
  harness: "gemini",
  parse: (ev) => ({
    sessionId: ev.session_id as string | undefined,
    transcriptPath: ev.transcript_path as string | undefined,
    cwd: ev.cwd as string | undefined,
  }),
  readTranscript: readGeminiTranscript,
});
