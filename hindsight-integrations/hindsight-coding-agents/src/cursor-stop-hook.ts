#!/usr/bin/env node
/** Hindsight Cursor CLI `stop` hook: retain the completed agent-turn transcript. */
import { runRetainHook } from "./core/retain-hook";
import { readCursorTranscript } from "./core/transcript-cursor";

void runRetainHook({
  harness: "cursor-cli",
  parse: (ev) => ({
    sessionId: (ev.conversation_id as string | undefined) ?? (ev.session_id as string | undefined),
    transcriptPath: ev.transcript_path as string | undefined,
    cwd:
      (ev.cwd as string | undefined) ??
      (ev.workspace_root as string | undefined) ??
      (Array.isArray(ev.workspace_roots)
        ? (ev.workspace_roots[0] as string | undefined)
        : undefined),
  }),
  readTranscript: readCursorTranscript,
});
