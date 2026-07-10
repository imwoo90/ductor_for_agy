"""Output parsing and log tracking for the Antigravity CLI (agy)."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from ductor_bot.cli.base import LogParser

logger = logging.getLogger(__name__)


def parse_antigravity_json(raw: str) -> str:
    """Extract result text from Antigravity CLI ``--print`` output.

    Tries to parse as JSON; falls back to raw text truncated to 2000 chars.
    """
    if not raw:
        return ""
    raw = raw.strip()
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            # Try common content keys
            for key in ("content", "result", "text", "message"):
                val = parsed.get(key)
                if isinstance(val, str) and val:
                    return val
            return str(parsed)
        return str(parsed)
    except json.JSONDecodeError:
        return raw[:2000]


class AntigravityLogParser(LogParser):
    """Encapsulates the agy-specific transcript.jsonl log parsing logic."""

    def is_session_active(self, session_id: str) -> bool:
        from ductor_bot.cli.antigravity_provider import AntigravityCLI
        return session_id in AntigravityCLI._session_holders

    def has_active_sessions(self) -> bool:
        from ductor_bot.cli.antigravity_provider import AntigravityCLI
        return bool(AntigravityCLI._session_holders)

    def get_transcript_path(self, session_id: str) -> Path:
        home = os.environ.get("USERPROFILE") or os.environ.get("HOME")
        base = Path(home) if home else Path.home()
        return base / ".gemini" / "antigravity-cli" / "brain" / session_id / ".system_generated" / "logs" / "transcript.jsonl"

    def parse_log_delta(
        self,
        session_id: str,
        transcript_path: Path,
        prev_size: int | None,
    ) -> tuple[int, str | None]:
        try:
            file_size = transcript_path.stat().st_size
        except OSError:
            return prev_size or 0, None

        from ductor_bot.cli.antigravity_provider import AntigravityCLI
        sync_in_progress = AntigravityCLI._sync_in_progress.get(session_id, False)
        sync_processed_size = AntigravityCLI._processed_log_sizes.get(str(transcript_path), 0)

        if prev_size is None:
            # Initialize count to current size or sync processed size, whichever is larger,
            # so we only check new lines added during this run
            return max(file_size, sync_processed_size), None

        if sync_processed_size > prev_size:
            prev_size = sync_processed_size

        if file_size < prev_size:
            # File was truncated or reset
            prev_size = 0

        if file_size <= prev_size:
            return prev_size, None

        try:
            with open(transcript_path, "r", encoding="utf-8", errors="replace") as f:
                f.seek(prev_size)
                new_content = f.read()
        except OSError:
            return prev_size, None

        entries = []
        for line in new_content.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
                if isinstance(entry, dict):
                    entries.append(entry)
            except json.JSONDecodeError:
                continue

        if not entries:
            return file_size, None

        thinking_blocks = []
        tool_calls = []
        tool_completions = []
        final_content = None

        for entry in entries:
            source = entry.get("source")
            etype = entry.get("type")
            status = entry.get("status")

            if source == "MODEL":
                if etype == "PLANNER_RESPONSE":
                    thinking = entry.get("thinking")
                    if thinking and isinstance(thinking, str) and thinking.strip():
                        thinking_blocks.append(thinking.strip())

                    tcalls = entry.get("tool_calls")
                    if tcalls and isinstance(tcalls, list):
                        for tc in tcalls:
                            name = tc.get("name", "unknown")
                            args = tc.get("args", {})
                            args_str = ", ".join(f"{k}={json.dumps(v, ensure_ascii=False)}" for k, v in args.items())
                            tool_calls.append(f"`{name}({args_str})`")

                    if status == "DONE" and not tcalls:
                        content = entry.get("content")
                        if content and isinstance(content, str) and content.strip():
                            if not sync_in_progress:
                                final_content = content.strip()

                elif etype in ("RUN_COMMAND", "VIEW_FILE", "LIST_DIRECTORY", "GREP_SEARCH", "GENERIC", "CODE_ACTION") and status == "DONE":
                    friendly_names = {
                        "RUN_COMMAND": "run_command (execute)",
                        "VIEW_FILE": "view_file (read)",
                        "LIST_DIRECTORY": "list_dir (list)",
                        "GREP_SEARCH": "grep_search (search)",
                        "CODE_ACTION": "replace_file_content (edit)",
                    }
                    name = friendly_names.get(etype, etype.lower())
                    tool_completions.append(f"`{name}` completed")

        parts = []
        if thinking_blocks:
            combined_thinking = "\n\n".join(thinking_blocks)
            blockquote_thinking = "\n".join(f">! {l}" for l in combined_thinking.splitlines())
            parts.append(f"💭 **Thinking Process:**\n{blockquote_thinking}")
        if tool_calls:
            calls_list = "\n".join(f">! • {tc}" for tc in tool_calls)
            parts.append(f"🛠️ **Tool Calls:**\n{calls_list}")
        if tool_completions:
            completions_list = "\n".join(f">! • {tc}" for tc in tool_completions)
            parts.append(f"📥 **Tool Completions:**\n{completions_list}")
        if final_content:
            parts.append(f"✅ **Final Response:**\n{final_content}")

        formatted_text = None
        if parts:
            header = "**[Ductor Background Completed]**" if final_content else "**[Ductor Background Progress]**"
            formatted_text = f"{header}\n\n" + "\n\n".join(parts)

        return file_size, formatted_text
