#!/usr/bin/env node
/** hindsight-gemini-sessionstart-hook — Gemini CLI `SessionStart` hook: deterministically starts a
 *  background seed of a cold repo's bank and injects the knowledge-page roster + tool guide. Mirrors
 *  the Claude/Codex entry points; harness is "gemini" so config/diag/bank resolution use the gemini
 *  identity. Gemini's SessionStart supports both `hookSpecificOutput.additionalContext` (model
 *  context) and `systemMessage` (user-visible), which runSessionStartHook already emits. */
import { runHarnessSessionStart } from "./harness/hook-lifecycle";

void runHarnessSessionStart("gemini");
