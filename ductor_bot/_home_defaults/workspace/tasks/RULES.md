# Background Tasks Directory

> [!WARNING]
> **For Antigravity (`antigravity` / `agy`) Provider:**
> This directory and its associated task tools are **DEPRECATED** and disabled on this ductor_for_agy branch.
> Instead, run long-running commands asynchronously using the native `run_command` (by setting a short `WaitMsBeforeAsync` and ending your turn), and manage subagents using native `define_subagent` / `invoke_subagent` tools.

This directory contains folders for active and completed background tasks.
Each subfolder holds a task's metadata (TASKMEMORY.md, rule files).

**Do not manually edit or create task folders here.**

## Managing tasks

Use the tools in `tools/task_tools/`:

- **Create**: `python3 tools/task_tools/create_task.py --name "..." "prompt"`
- **List**: `python3 tools/task_tools/list_tasks.py`
- **Cancel**: `python3 tools/task_tools/cancel_task.py TASK_ID`
- **Resume**: `python3 tools/task_tools/resume_task.py TASK_ID "follow-up"`

See `tools/task_tools/CLAUDE/GEMINI/AGENTS.md` for full documentation.
