# Cron Tasks

This directory contains isolated task folders used by scheduled jobs.
For cron tool commands (add/edit/remove/list), see `tools/cron_tools/CLAUDE.md`.

## MANDATORY WORKFLOW: Creating Cron Jobs

**CRITICAL: When creating a new cron job, you MUST ALWAYS ask the user these questions:**

1. **Which model?** (`--model <name>`)
   - Options:
     - `Gemini 3.5 Flash (High)` - Fast, highly capable agentic model (recommended)
     - `Gemini 3.5 Pro (High)` - Most capable reasoning model
     - `Gemini 3.5 Flash (Low)` - Fast, low rate limit/cost model
     - `Gemini 3.5 Pro (Low)` - Capable reasoning, low rate limit/cost model
   - Default if user doesn't specify: Use global config model

**YOU MUST present these options to the user and wait for their answers BEFORE calling cron_add.py!**

**Advanced: CLI Parameters**
If the user explicitly requests additional CLI flags, use `--cli-parameters '<json-array>'`.
DO NOT suggest this proactively - only use if the user asks for it.

**Example conversation flow:**

User: "Create a cron job to check weather every 3 minutes"

You: "I'll create a cron job to check weather every 3 minutes. Let me configure the execution:

**Model**: Which Antigravity model should execute this task?
   - `Gemini 3.5 Flash (High)` (fast, highly capable - recommended)
   - `Gemini 3.5 Pro (High)` (most capable reasoning model)
   - `Gemini 3.5 Flash (Low)` (low rate limit/cost model)
   - `Gemini 3.5 Pro (Low)` (low rate limit/cost reasoning model)

Please specify your choice, or I'll use the global config default."

[Wait for user response, then call cron_add.py with appropriate flags]

## Rules for Task Execution

1. Avoid modifying directories directly under `cron_tasks/` except via cron tools.
2. Read the `TASK_DESCRIPTION.md` file in the task's subdirectory to understand what the task does.
3. If changing the code or behavior of an existing cron task, edit `cron_tasks/<task-name>/TASK_DESCRIPTION.md` to keep documentation accurate.
