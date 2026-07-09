# Background Tasks (DEPRECATED for Gemini/agy)

> [!WARNING]
> The Ductor-level task tools (`create_task.py`, `cancel_task.py`, `resume_task.py`) and task directories are **DEPRECATED** and disabled on this custom branch for Gemini/agy. 

Thanks to the **PTY Session Warm-Loading & Log Monitoring** architecture implemented on this branch, you should use native tools instead:

1. **For Asynchronous/Long-running Commands:**
   - Use the native `run_command` tool.
   - Set `WaitMsBeforeAsync` to a small value (e.g. `5000` or less) to send the command to the background.
   - End your turn by calling no more tools.
   - The background Log Watcher daemon will automatically parse your thoughts and tool execution from `transcript.jsonl` and push updates (`💭 생각 흐름`, `🛠️ 도구 호출`, `📥 도구 완료`) directly to the Telegram room.

2. **For Multi-Agent Cooperation:**
   - Use the native `define_subagent` and `invoke_subagent` tools instead of spawning separate Ductor background tasks.
   - Communicate with running subagents using `send_message`.
