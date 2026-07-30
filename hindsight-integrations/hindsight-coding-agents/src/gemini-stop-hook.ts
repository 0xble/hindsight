#!/usr/bin/env node
/** hindsight-gemini-stop-hook — Gemini CLI `SessionEnd` hook: writes the session's transcript back
 *  to memory. Same runtime as the Claude/Codex Stop hooks, but with the Gemini JSONL reader. Gemini's
 *  SessionEnd stdin carries only `reason`, but every hook also gets `transcript_path` — the file the
 *  reader parses. */
import { runHarnessRetain } from "./harness/hook-lifecycle";

void runHarnessRetain("gemini");
