# Background Tasks Directory (DEPRECATED)

> [!WARNING]
> This directory and its associated Ductor-level task tools are **DEPRECATED** on this custom branch.

Do not use `create_task.py`, `cancel_task.py`, or `resume_task.py`. 

Instead:
* Run long-running terminal commands asynchronously using the native `run_command` (by setting a short `WaitMsBeforeAsync` time and ending your turn).
* Spawn collaborative agents using the native `define_subagent` and `invoke_subagent` tools.
* The system's warm PTY session and active Log Watcher daemon will handle all background progress monitoring and Telegram updates automatically.
