#!/usr/bin/env node
/** hindsight-claude-sessionstart-hook — offers to seed a cold repo's bank on session start. */
import { runSessionStartHook } from "./core/session-start";

void runSessionStartHook();
