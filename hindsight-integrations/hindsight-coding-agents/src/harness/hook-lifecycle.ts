/**
 * The single lifecycle contract for every hook-based harness. Entry-point binaries, installer
 * wiring, payload parsing, host response encoding, and transcript readers all resolve through
 * this registry. Adding a harness means adding one complete declaration here; its lifecycle
 * cannot silently drift between the runtime and installer.
 */
import { runHook, type HookSpec } from "../core/hook";
import { runRetainHook, type RetainHookSpec } from "../core/retain-hook";
import { runSessionStartHook, type SessionStartHookSpec } from "../core/session-start";
import { readCodexTranscript } from "../core/transcript-codex";
import { readCursorTranscript } from "../core/transcript-cursor";
import { readGeminiTranscript } from "../core/transcript-gemini";

export type HookHarnessName = "claude-code" | "codex" | "gemini" | "cursor-cli";
export type HookLifecycle = "sessionStart" | "prompt" | "stop";
export type HookConfigStyle = "nested" | "flat";

export interface HookInstallSpec {
  event: string;
  entry: string;
  timeout?: number;
}

export interface HookHarnessSpec {
  configStyle: HookConfigStyle;
  install: Record<HookLifecycle, HookInstallSpec>;
  sessionStart: SessionStartHookSpec;
  prompt: HookSpec;
  retain: RetainHookSpec;
}

const cursorCwd = (ev: Record<string, unknown>): string | undefined =>
  (ev.cwd as string | undefined) ??
  (ev.workspace_root as string | undefined) ??
  (Array.isArray(ev.workspace_roots) ? (ev.workspace_roots[0] as string | undefined) : undefined);

const claudePrompt: HookSpec = {
  harness: "claude-code",
  parse: (ev) => ({
    prompt: ev.prompt as string | undefined,
    cwd: ev.cwd as string | undefined,
    sessionId: ev.session_id as string | undefined,
  }),
  emit: (context, notice) => ({
    ...(notice ? { systemMessage: notice } : {}),
    hookSpecificOutput: { hookEventName: "UserPromptSubmit", additionalContext: context },
  }),
};

const codexPrompt: HookSpec = {
  ...claudePrompt,
  harness: "codex",
  parse: (ev) => ({
    prompt: (ev.prompt as string | undefined) ?? (ev.user_prompt as string | undefined),
    cwd: ev.cwd as string | undefined,
    sessionId: ev.session_id as string | undefined,
  }),
};

const geminiPrompt: HookSpec = {
  harness: "gemini",
  parse: (ev) => ({
    prompt: ev.prompt as string | undefined,
    cwd: ev.cwd as string | undefined,
    sessionId: ev.session_id as string | undefined,
  }),
  emit: (context, notice) => ({
    ...(notice ? { systemMessage: notice } : {}),
    hookSpecificOutput: { hookEventName: "BeforeAgent", additionalContext: context },
  }),
};

const cursorPrompt: HookSpec = {
  harness: "cursor-cli",
  parse: (ev) => ({
    prompt: (ev.prompt as string | undefined) ?? (ev.user_prompt as string | undefined),
    cwd: cursorCwd(ev),
    sessionId: (ev.conversation_id as string | undefined) ?? (ev.session_id as string | undefined),
  }),
  emit: (context) => ({ continue: true, additional_context: context }),
};

const standardSessionStart = (harness: string): SessionStartHookSpec => ({
  harness,
  emit: (out) => ({
    ...(out.systemMessage ? { systemMessage: out.systemMessage } : {}),
    hookSpecificOutput: {
      hookEventName: "SessionStart",
      ...(out.additionalContext ? { additionalContext: out.additionalContext } : {}),
    },
  }),
});

export const HOOK_HARNESSES: Record<HookHarnessName, HookHarnessSpec> = {
  "claude-code": {
    configStyle: "nested",
    install: {
      sessionStart: { event: "SessionStart", entry: "claude-sessionstart-hook.js", timeout: 30 },
      prompt: { event: "UserPromptSubmit", entry: "claude-hook.js", timeout: 30 },
      stop: { event: "Stop", entry: "claude-stop-hook.js", timeout: 60 },
    },
    sessionStart: standardSessionStart("claude-code"),
    prompt: claudePrompt,
    retain: {
      harness: "claude-code",
      parse: (ev) => ({
        sessionId: ev.session_id as string | undefined,
        transcriptPath: ev.transcript_path as string | undefined,
        cwd: ev.cwd as string | undefined,
      }),
    },
  },
  codex: {
    configStyle: "nested",
    install: {
      sessionStart: { event: "SessionStart", entry: "codex-sessionstart-hook.js", timeout: 30 },
      prompt: { event: "UserPromptSubmit", entry: "codex-hook.js", timeout: 30 },
      stop: { event: "Stop", entry: "codex-stop-hook.js", timeout: 60 },
    },
    sessionStart: standardSessionStart("codex"),
    prompt: codexPrompt,
    retain: {
      harness: "codex",
      parse: (ev) => ({
        sessionId: ev.session_id as string | undefined,
        transcriptPath: ev.transcript_path as string | undefined,
        cwd: ev.cwd as string | undefined,
      }),
      readTranscript: readCodexTranscript,
    },
  },
  gemini: {
    configStyle: "nested",
    install: {
      sessionStart: { event: "SessionStart", entry: "gemini-sessionstart-hook.js", timeout: 15000 },
      prompt: { event: "BeforeAgent", entry: "gemini-hook.js", timeout: 15000 },
      stop: { event: "SessionEnd", entry: "gemini-stop-hook.js", timeout: 30000 },
    },
    sessionStart: standardSessionStart("gemini"),
    prompt: geminiPrompt,
    retain: {
      harness: "gemini",
      parse: (ev) => ({
        sessionId: ev.session_id as string | undefined,
        transcriptPath: ev.transcript_path as string | undefined,
        cwd: ev.cwd as string | undefined,
      }),
      readTranscript: readGeminiTranscript,
    },
  },
  "cursor-cli": {
    configStyle: "flat",
    install: {
      sessionStart: { event: "sessionStart", entry: "cursor-sessionstart-hook.js", timeout: 30 },
      prompt: { event: "beforeSubmitPrompt", entry: "cursor-hook.js" },
      stop: { event: "stop", entry: "cursor-stop-hook.js", timeout: 30 },
    },
    sessionStart: {
      harness: "cursor-cli",
      emit: (out) => ({
        ...(out.additionalContext ? { additional_context: out.additionalContext } : {}),
      }),
    },
    prompt: cursorPrompt,
    retain: {
      harness: "cursor-cli",
      parse: (ev) => ({
        sessionId:
          (ev.conversation_id as string | undefined) ?? (ev.session_id as string | undefined),
        transcriptPath: ev.transcript_path as string | undefined,
        cwd: cursorCwd(ev),
      }),
      readTranscript: readCursorTranscript,
    },
  },
};

export const runHarnessSessionStart = (harness: HookHarnessName): Promise<void> =>
  runSessionStartHook(HOOK_HARNESSES[harness].sessionStart);
export const runHarnessPrompt = (harness: HookHarnessName): Promise<void> =>
  runHook(HOOK_HARNESSES[harness].prompt);
export const runHarnessRetain = (harness: HookHarnessName): Promise<void> =>
  runRetainHook(HOOK_HARNESSES[harness].retain);
