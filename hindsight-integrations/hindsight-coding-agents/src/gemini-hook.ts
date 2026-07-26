#!/usr/bin/env node
/**
 * hindsight-gemini-hook — the Gemini CLI entry point (a `BeforeAgent` hook).
 *
 * Gemini CLI (0.52.0+) has a Claude-Code-style hooks system (stdin/stdout JSON). The per-turn
 * recall hook is `BeforeAgent` (Gemini's name for what Claude/Codex call UserPromptSubmit): stdin
 * carries `prompt` + `cwd` + `session_id`; injection rides `hookSpecificOutput.additionalContext`,
 * which Gemini appends to the prompt for that turn.
 *
 * Install (~/.gemini/settings.json):
 *   { "hooks": { "BeforeAgent": [ { "hooks": [
 *       { "type": "command", "command": "node .../gemini-hook.js" } ] } ] } }
 *
 * Behavior (shared hook runtime, core/hook.ts): recall every prompt, inject the memories block;
 * outcomes recorded in the diagnostic file. Config/diag/bank resolution use the harness name "gemini".
 */
import { runHook } from "./core/hook";

void runHook({
  harness: "gemini",
  parse: (ev) => ({
    prompt: ev.prompt as string | undefined,
    cwd: ev.cwd as string | undefined,
    sessionId: ev.session_id as string | undefined,
  }),
  emit: (context) => ({
    hookSpecificOutput: { hookEventName: "BeforeAgent", additionalContext: context },
  }),
});
