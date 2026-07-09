"""Async wrapper around the Antigravity CLI (agy)."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import AsyncGenerator, Mapping
from pathlib import Path
from typing import TYPE_CHECKING

from ductor_bot.cli.antigravity_events import parse_antigravity_json
from ductor_bot.cli.antigravity_runtime import antigravity_process_env
from ductor_bot.cli.base import BaseCLI, CLIConfig, LogParser
from ductor_bot.cli.executor import build_subprocess_env
from ductor_bot.cli.process_registry import ProcessRegistry, TrackedProcess
from ductor_bot.cli.stream_events import AssistantTextDelta, ResultEvent, StreamEvent
from ductor_bot.cli.types import CLIResponse
from ductor_bot.config import ANTIGRAVITY_MODELS
from ductor_bot.infra.platform import CREATION_FLAGS as _CREATION_FLAGS
from ductor_bot.infra.process_tree import force_kill_process_tree

# Reuse the cross-platform directory-link helper (symlink with Windows junction
# fallback) to work around agy's hidden-dotted-workspace bug; see
# _safe_agy_workspace below.
from ductor_bot.workspace.skill_sync import _create_dir_link

if TYPE_CHECKING:
    from ductor_bot.cli.timeout_controller import TimeoutController

logger = logging.getLogger(__name__)

_DEFAULT_TIMEOUT = 300.0  # 5 minutes, matches agy --print-timeout default

import time

class SessionHolder:
    def __init__(self, proc, master_fd: int, reader_task: asyncio.Task) -> None:
        self.proc = proc
        self.master_fd = master_fd
        self.reader_task = reader_task
        self.last_active = time.time()


async def _pty_drain_loop(master_fd: int, proc) -> None:
    """Read and discard output from the master PTY fd to prevent buffering hangs."""
    while proc.poll() is None:
        try:
            # Read asynchronously in worker thread
            data = await asyncio.to_thread(os.read, master_fd, 4096)
            if not data:
                break
        except OSError:
            break
        except Exception as e:
            logger.debug("Error in PTY drain loop: %s", e)
            break


def _trust_workspace_in_settings(workspace: Path, env: Mapping[str, str] | None = None) -> None:
    """Ensure *workspace* is added to settings.json's trustedWorkspaces."""
    import json
    home_dir = Path.home()
    if env and "HOME" in env:
        home_dir = Path(env["HOME"])
    
    settings_file = home_dir / ".gemini" / "antigravity-cli" / "settings.json"
    if not settings_file.parent.is_dir():
        return
        
    try:
        data = {}
        if settings_file.is_file():
            try:
                data = json.loads(settings_file.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                data = {}
            
        workspaces = data.setdefault("trustedWorkspaces", [])
        ws_str = str(workspace.resolve())
        if ws_str not in workspaces:
            workspaces.append(ws_str)
            settings_file.write_text(json.dumps(data, indent=2), encoding="utf-8")
            logger.info("Automatically trusted workspace %s in settings.json", ws_str)
    except Exception as e:
        logger.warning("Failed to auto-trust workspace: %s", e)


class AntigravityCLI(BaseCLI):
    """Async wrapper around the Antigravity CLI (agy).

    agy has no headless streaming protocol: ``--print`` is one-shot and
    ``--prompt-interactive`` is a bubbletea TUI that needs a real ``/dev/tty``
    a subprocess does not have. Both :meth:`send` and :meth:`send_streaming`
    drive the same ``--print`` command.

    ``agy --print`` also silently drops its stdout when stdout is not a TTY
    (pipe/subprocess/redirect) -- upstream bug
    ``google-antigravity/antigravity-cli#76``. The answer is therefore read
    back from agy's own per-conversation transcript
    (``<home>/.gemini/antigravity-cli/brain/<conv-id>/.system_generated/logs/transcript.jsonl``),
    taking the final ``source=MODEL, type=PLANNER_RESPONSE, status=DONE``
    entry's ``content`` -- the clean answer without the intermediate tool-call
    narration. stdout is used only as a fallback.

    agy flags reference:
      --print / -p <prompt>   Non-interactive single-shot
      --continue / -c         Continue most recent conversation
      --conversation <id>     Resume a specific conversation
      --dangerously-skip-permissions  Auto-approve all tools
      --print-timeout <dur>   Timeout for --print mode (default 5m)
      --model <id>            Select a model (see ``agy models``)
      --add-dir <dir>         Add workspace directory
      --log-file <path>       Override log file
    """
    _session_holders: dict[str, SessionHolder] = {}
    _processed_log_sizes: dict[str, int] = {}
    _sync_in_progress: dict[str, bool] = {}
    _shutting_down = False
    _monitor_task: asyncio.Task[None] | None = None

    @classmethod
    def shutdown_class(cls) -> None:
        """Kills active Antigravity session holder(s) on shutdown."""
        import os
        import signal
        cls._shutting_down = True
        if cls._monitor_task:
            cls._monitor_task.cancel()
            cls._monitor_task = None
        holders = list(cls._session_holders.items())
        if holders:
            logger.info("Shutdown killing %d active Antigravity session holder(s)", len(holders))
            for session_id, holder in holders:
                holder.reader_task.cancel()
                try:
                    if holder.proc.poll() is None:
                        pgid = os.getpgid(holder.proc.pid)
                        if pgid != os.getpgrp():
                            os.killpg(pgid, signal.SIGKILL)
                        else:
                            holder.proc.kill()
                        holder.proc.wait(timeout=1.0)
                except Exception as e:
                    logger.debug("Error killing process group for session %s: %s", session_id, e)
                try:
                    os.close(holder.master_fd)
                except OSError:
                    pass
            cls._session_holders.clear()

    @classmethod
    async def _run_monitor_loop(cls) -> None:
        import gc
        from ductor_bot.orchestrator.core import Orchestrator
        from ductor_bot.bus.bus import MessageBus
        from ductor_bot.bus.envelope import Envelope, Origin

        logger.info("Antigravity background log monitor loop started")

        while not cls._shutting_down:
            try:
                await asyncio.sleep(5.0)

                orch = None
                bus = None
                for obj in gc.get_objects():
                    if orch is None and isinstance(obj, Orchestrator):
                        orch = obj
                    if bus is None and isinstance(obj, MessageBus):
                        bus = obj
                    if orch is not None and bus is not None:
                        break

                if orch is None or bus is None:
                    continue

                sessions = await orch._sessions.list_all()
                for session in sessions:
                    chat_id = session.chat_id
                    topic_id = session.topic_id
                    transport = getattr(session, "transport", "tg")

                    provider_name = session.provider
                    if provider_name != "antigravity":
                        continue

                    provider_data = session.provider_sessions.get(provider_name)
                    if not provider_data or not provider_data.session_id:
                        continue

                    session_id = provider_data.session_id

                    parser = AntigravityLogParser()
                    if not parser.is_session_active(session_id):
                        continue

                    transcript_path = parser.get_transcript_path(session_id)
                    if not transcript_path.is_file():
                        continue

                    prev_size = cls._processed_log_sizes.get(str(transcript_path))
                    new_size, formatted_text = parser.parse_log_delta(
                        session_id, transcript_path, prev_size
                    )

                    if new_size is not None:
                        cls._processed_log_sizes[str(transcript_path)] = new_size

                    if formatted_text:
                        logger.info(
                            "Antigravity Log Monitor: Submitting log delta to bus for chat %s topic %s",
                            chat_id,
                            topic_id,
                        )
                        envelope = Envelope(
                            origin=Origin.BACKGROUND,
                            chat_id=chat_id,
                            topic_id=topic_id,
                            transport=transport,
                            result_text=formatted_text,
                        )
                        await bus.submit(envelope)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Error in Antigravity log monitor loop: %s", e)

    def __init__(self, config: CLIConfig) -> None:
        self._config = config
        self._working_dir = Path(config.working_dir).resolve()
        self._cli = "agy"
        self._agy_workspace_cache: Path | None = None
        logger.info("AntigravityCLI: cwd=%s model=%s", self._working_dir, config.model)

        try:
            loop = asyncio.get_running_loop()
            if self.__class__._monitor_task is None or self.__class__._monitor_task.done():
                self.__class__._monitor_task = loop.create_task(self.__class__._run_monitor_loop())
                logger.info("Antigravity background log monitor task started")
        except RuntimeError:
            logger.debug("No running event loop, skipping background log monitor task creation")

    @classmethod
    def _cleanup_expired_holders(cls) -> None:
        import time
        import signal
        import os
        now = time.time()
        expired_keys = []
        for session_id, holder in list(cls._session_holders.items()):
            # 24 hours = 86400 seconds
            if now - holder.last_active > 86400.0 or holder.proc.poll() is not None:
                expired_keys.append(session_id)
                logger.info("Cleaning up session holder for %s", session_id)
                holder.reader_task.cancel()
                try:
                    if holder.proc.poll() is None:
                        pgid = os.getpgid(holder.proc.pid)
                        if pgid != os.getpgrp():
                            os.killpg(pgid, signal.SIGTERM)
                        else:
                            holder.proc.terminate()
                        holder.proc.wait(timeout=2.0)
                except Exception as e:
                    logger.debug("Error killing process group for %s: %s", session_id, e)
                try:
                    os.close(holder.master_fd)
                except OSError:
                    pass
        for key in expired_keys:
            cls._session_holders.pop(key, None)

    @classmethod
    def _ensure_session_holder(
        cls,
        session_id: str,
        workspace: Path,
        env: dict,
        permission_mode: str | None = None,
    ) -> None:
        if getattr(cls, "_shutting_down", False):
            return
        import time
        import pty
        import subprocess
        import os
        
        cls._cleanup_expired_holders()
        
        # Check if the holder is already running and active
        holder = cls._session_holders.get(session_id)
        if holder is not None:
            # Check if process is still running
            if holder.proc.poll() is None:
                # Update last active time
                holder.last_active = time.time()
                logger.debug("Session holder for %s is already active", session_id)
                return
            else:
                logger.warning("Session holder for %s was dead, restarting", session_id)
                holder.reader_task.cancel()
                try:
                    os.close(holder.master_fd)
                except OSError:
                    pass
                cls._session_holders.pop(session_id, None)

        # Natively trust the workspace folder
        _trust_workspace_in_settings(workspace, env)

        # Spawn a new session holder
        logger.info("Spawning new session holder for %s", session_id)
        cmd = [
            "agy",
            "--add-dir", str(workspace),
            "--conversation", session_id,
        ]
        if permission_mode == "bypassPermissions":
            cmd.append("--dangerously-skip-permissions")
        cmd.extend(["--prompt-interactive", ""])

        try:
            import termios
            master_fd, slave_fd = pty.openpty()
            try:
                attrs = termios.tcgetattr(slave_fd)
                attrs[3] = attrs[3] & ~termios.ECHO
                termios.tcsetattr(slave_fd, termios.TCSANOW, attrs)
            except Exception as e:
                logger.warning("Failed to disable PTY echo: %s", e)

            proc = subprocess.Popen(
                cmd,
                stdin=slave_fd,
                stdout=slave_fd,
                stderr=slave_fd,
                close_fds=True,
                cwd=str(workspace),
                env=env,
                preexec_fn=os.setsid
            )
            os.close(slave_fd)
            
            # Spawn background reader task to drain PTY buffer
            reader_task = asyncio.create_task(_pty_drain_loop(master_fd, proc))
            cls._session_holders[session_id] = SessionHolder(proc, master_fd, reader_task)
            logger.info("Spawned session holder for %s (PID=%d)", session_id, proc.pid)
        except Exception as e:
            logger.error("Failed to spawn session holder for %s: %s", session_id, e)

    def get_log_parser(self) -> LogParser | None:
        """Return the AntigravityLogParser."""
        return AntigravityLogParser()

    @property
    def _agy_workspace(self) -> Path:
        """Path agy accepts as a workspace; resolved lazily on first use.

        agy rejects any workspace whose path has a dot-prefixed ancestor (e.g.
        ~/.ductor/workspace) and falls back to its scratch sandbox, so the
        workspace is exposed through a non-dotted symlink. That symlink is
        created only here -- when agy is actually about to run -- so it never
        appears for users who only use claude/codex/gemini.
        """
        if self._agy_workspace_cache is None:
            self._agy_workspace_cache = _safe_agy_workspace(self._working_dir)
        return self._agy_workspace_cache

    def _build_command(
        self,
        prompt: str,
        *,
        resume_session: str | None = None,
        continue_session: bool = False,
    ) -> list[str]:
        """Build the full ``agy`` command.

        ``--print`` is a string flag that consumes the *next* token as the
        prompt, so it must come last with the prompt immediately after it.
        Otherwise ``agy --print --model X <prompt>`` makes agy treat
        ``--model`` as the prompt and silently drops both the real prompt and
        the requested model (falling back to its default).
        """
        cmd = [self._cli]

        # Ground agy in ductor's per-agent workspace so its tools operate there
        # instead of falling back to agy's own scratch sandbox
        # (~/.gemini/antigravity-cli/scratch). Because agy keys conversations by
        # cwd, this also keeps main agent, sub-agents and topics that use
        # distinct working dirs isolated -- and matches the cwd the transcript
        # reader resolves the answer from.
        cmd += ["--add-dir", str(self._agy_workspace)]

        if self._config.model and self._config.model not in ANTIGRAVITY_MODELS:
            cmd += ["--model", self._config.model]

        # Session resume / continue
        if resume_session:
            cmd += ["--conversation", resume_session]
        elif continue_session:
            cmd += ["--continue"]

        # Auto-approve when bypass mode is set
        if self._config.permission_mode == "bypassPermissions":
            cmd += ["--dangerously-skip-permissions"]

        cmd.extend(self._config.cli_parameters)

        # --print and its prompt value MUST be last and adjacent (see docstring).
        cmd += ["--print", prompt]
        return cmd

    def _host_command(self, cmd: list[str]) -> tuple[list[str], str]:
        """Return a host-execution command.

        Antigravity is a host CLI. The standard Docker sandbox image does not
        include ``agy`` or its user auth state, so running it through
        ``docker exec`` produces an OCI "agy not found" error.
        """
        if self._config.docker_container:
            logger.info("Antigravity runs on host; ignoring Docker container for agy")
        return cmd, str(self._agy_workspace)

    # -- Process tracking -----------------------------------------------------

    def _track_process(
        self,
        process: asyncio.subprocess.Process,
    ) -> tuple[ProcessRegistry | None, TrackedProcess | None]:
        """Register a subprocess in ProcessRegistry if tracking is enabled."""
        reg = self._config.process_registry
        tracked = (
            reg.register(
                self._config.chat_id,
                process,
                self._config.process_label,
                topic_id=self._config.topic_id,
            )
            if reg
            else None
        )
        return reg, tracked

    @staticmethod
    def _untrack_process(reg: ProcessRegistry | None, tracked: TrackedProcess | None) -> None:
        """Unregister a previously tracked subprocess."""
        if tracked is not None and reg is not None:
            reg.unregister(tracked)

    # -- Non-streaming --------------------------------------------------------

    async def send(
        self,
        prompt: str,
        resume_session: str | None = None,
        continue_session: bool = False,
        timeout_seconds: float | None = None,
        timeout_controller: TimeoutController | None = None,
    ) -> CLIResponse:
        """Send a prompt via ``agy --print`` and return the full response."""
        effective_timeout = timeout_seconds or _DEFAULT_TIMEOUT
        env = antigravity_process_env(build_subprocess_env(self._config))

        if resume_session is None and not continue_session:
            import uuid
            resume_session = str(uuid.uuid4())

        requested_session_existed = False
        if resume_session:
            session_id = resume_session
            brain_dir = _agy_state_root(env) / "brain" / session_id
            if brain_dir.is_dir():
                requested_session_existed = True
        else:
            brain_dir = _resolve_brain_dir(self._agy_workspace, env)
            session_id = brain_dir.name if brain_dir is not None else None

        cmd = self._build_command(
            prompt,
            resume_session=resume_session,
            continue_session=continue_session,
        )

        cmd, cwd = self._host_command(cmd)
        safe_cmd = _safe_command_for_logging(cmd)
        logger.debug("Antigravity send: %s", safe_cmd)

        if session_id:
            self.__class__._sync_in_progress[session_id] = True

        try:
            if resume_session:
                self._ensure_session_holder(resume_session, self._agy_workspace, env, self._config.permission_mode)

            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
                env=env,
                creationflags=_CREATION_FLAGS,
            )

            reg, tracked = self._track_process(proc)

            try:
                timed_out = False
                try:
                    if timeout_controller is not None:
                        returncode = await timeout_controller.run_with_timeout(
                            proc.wait()
                        )
                    else:
                        async with asyncio.timeout(effective_timeout):
                            returncode = await proc.wait()
                    
                    # Read stdout/stderr with a short timeout to prevent hanging on open pipe FDs
                    stdout_bytes = b""
                    stderr_bytes = b""
                    try:
                        async with asyncio.timeout(0.5):
                            if proc.stdout is not None:
                                stdout_bytes = await proc.stdout.read()
                    except TimeoutError:
                        logger.debug("Stdout read timed out, ignoring")
                    try:
                        async with asyncio.timeout(0.5):
                            if proc.stderr is not None:
                                stderr_bytes = await proc.stderr.read()
                    except TimeoutError:
                        logger.debug("Stderr read timed out, ignoring")
                except TimeoutError:
                    timed_out = True
                    logger.warning("Antigravity send timed out")
                    force_kill_process_tree(proc.pid)
                    try:
                        async with asyncio.timeout(2.0):
                            stdout_bytes, stderr_bytes = await proc.communicate()
                    except TimeoutError:
                        logger.warning("proc.communicate() timed out after kill, forcing empty buffers")
                        stdout_bytes, stderr_bytes = b"", b""
                    return CLIResponse(
                        result="Timeout",
                        is_error=True,
                        timed_out=True,
                        returncode=proc.returncode,
                        stderr=stderr_bytes.decode(errors="replace")[:2000] if stderr_bytes else "",
                    )
            finally:
                self._untrack_process(reg, tracked)
                if not timed_out and proc.returncode is None:
                    force_kill_process_tree(proc.pid)

            stdout = stdout_bytes.decode(errors="replace") if stdout_bytes else ""
            stderr = stderr_bytes.decode(errors="replace") if stderr_bytes else ""

            # Resolve the actual brain directory after the run, in case agy generated a new UUID
            actual_brain_dir = None
            if resume_session and requested_session_existed:
                actual_brain_dir = brain_dir
            else:
                actual_brain_dir = _resolve_brain_dir(self._agy_workspace, env)

            if actual_brain_dir is not None:
                brain_dir = actual_brain_dir
                session_id = brain_dir.name

            # agy --print silently drops stdout in non-TTY subprocesses (upstream
            # bug antigravity-cli#76), so prefer agy's own transcript file, which
            # also yields the clean final answer without tool-call narration.
            # stdout is the fallback for environments/versions where it works.
            transcript_answer = _read_transcript_answer(self._agy_workspace, env, brain_dir=brain_dir)
            if transcript_answer is not None:
                logger.debug("Antigravity answer read from transcript")
                result_text = transcript_answer
            else:
                result_text = parse_antigravity_json(stdout)
            is_error = proc.returncode not in (None, 0)

            if session_id:
                if resume_session and session_id != resume_session:
                    holder = self.__class__._session_holders.pop(resume_session, None)
                    if holder is not None:
                        self.__class__._session_holders[session_id] = holder
                self._ensure_session_holder(session_id, self._agy_workspace, env, self._config.permission_mode)
                
                transcript_path = brain_dir / ".system_generated" / "logs" / "transcript.jsonl"
                if transcript_path.is_file():
                    self.__class__._processed_log_sizes[str(transcript_path)] = transcript_path.stat().st_size

            return CLIResponse(
                session_id=session_id,
                result=result_text,
                is_error=is_error,
                returncode=proc.returncode,
                stderr=stderr,
            )
        finally:
            if session_id:
                self.__class__._sync_in_progress[session_id] = False

    # -- Streaming ------------------------------------------------------------

    async def send_streaming(
        self,
        prompt: str,
        resume_session: str | None = None,
        continue_session: bool = False,
        timeout_seconds: float | None = None,
        timeout_controller: TimeoutController | None = None,
    ) -> AsyncGenerator[StreamEvent, None]:
        """Stream a response from agy.

        agy exposes no incremental stream, so this runs the one-shot
        ``--print`` path and emits the answer as a single text delta followed
        by the final result event. This keeps the streaming contract intact
        for the orchestrator while matching what the CLI can actually do.
        """
        response = await self.send(
            prompt,
            resume_session=resume_session,
            continue_session=continue_session,
            timeout_seconds=timeout_seconds,
            timeout_controller=timeout_controller,
        )

        if response.result:
            yield AssistantTextDelta(type="assistant", text=response.result)

        yield ResultEvent(
            type="result",
            result=response.result,
            is_error=response.is_error,
            returncode=response.returncode,
            session_id=response.session_id,
        )


# -- Module-level helpers -----------------------------------------------------


def _safe_command_for_logging(cmd: list[str]) -> list[str]:
    """Return a command safe for debug logs."""
    safe = [part if len(part) <= 80 else part[:80] + "..." for part in cmd]
    if "--print" in cmd and safe:
        safe[-1] = "<prompt>"
    return safe


# -- Workspace path (workaround for antigravity-cli#20) ------------------------
#
# agy rejects any workspace folder whose path contains a dot-prefixed ancestor
# ("... is hidden: ignore uri") and silently falls back to its scratch sandbox,
# so ductor's ~/.ductor/workspace is never accepted. agy checks the literal path
# it is given, not the resolved target, so a non-dotted directory symlink to the
# real workspace works around it.
# See https://github.com/google-antigravity/antigravity-cli/issues/20


def _safe_agy_workspace(working_dir: Path) -> Path:
    """Return a path agy will accept as a workspace for *working_dir*.

    If *working_dir* has a dot-prefixed ancestor, expose it via a non-dotted
    sibling symlink and return the symlinked path. Falls back to *working_dir*
    if there is no such ancestor or the symlink cannot be created (agy then uses
    its scratch sandbox -- degraded, not broken).
    """
    if "/." not in working_dir.as_posix():
        return working_dir

    parts = working_dir.parts
    for index, segment in enumerate(parts):
        if segment.startswith(".") and segment not in (".", ".."):
            dot_ancestor = Path(*parts[: index + 1])
            link = dot_ancestor.with_name(segment[1:])  # strip the leading dot
            remainder = Path(*parts[index + 1 :]) if index + 1 < len(parts) else Path()
            if _ensure_agy_link(link, dot_ancestor):
                return link / remainder
            return working_dir
    return working_dir


def _ensure_agy_link(link: Path, target: Path) -> bool:
    """Idempotently point the non-dotted *link* at *target*; return success."""
    try:
        if link.is_symlink():
            if link.resolve() == target.resolve():
                return True
            link.unlink()
        elif link.exists():
            # A real directory occupies the path -- never clobber it.
            logger.warning("Antigravity: %s exists and is not a symlink; using scratch", link)
            return False
        link.parent.mkdir(parents=True, exist_ok=True)
        _create_dir_link(link, target)
    except OSError as exc:
        logger.warning("Antigravity: could not link %s -> %s (%s)", link, target, exc)
        return False
    return link.exists()


# -- Transcript reading (workaround for antigravity-cli#76) --------------------
#
# ``agy --print`` completes the model round-trip but writes nothing to stdout
# when stdout is not a TTY (pipe/subprocess). It persists the full turn to a
# per-conversation JSONL transcript instead, so the answer is read from there.
# See https://github.com/google-antigravity/antigravity-cli/issues/76


def _agy_state_root(env: Mapping[str, str] | None = None) -> Path:
    """Locate agy's per-user state dir, cross-platform.

    agy stores conversations under ``<home>/.gemini/antigravity-cli`` where
    ``<home>`` is the user's home directory on every platform -- ``HOME`` on
    Linux/macOS, ``USERPROFILE`` on Windows. It is derived from the same
    environment handed to the agy subprocess so ductor reads exactly where agy
    wrote, falling back to the current user's home.
    """
    source = env if env is not None else os.environ
    home = source.get("USERPROFILE") or source.get("HOME")
    base = Path(home) if home else Path.home()
    return base / ".gemini" / "antigravity-cli"


def _read_transcript_answer(
    working_dir: Path,
    env: Mapping[str, str] | None = None,
    brain_dir: Path | None = None,
) -> str | None:
    """Return agy's final answer for *working_dir* from its transcript, or None.

    The answer is the last ``source=MODEL, type=PLANNER_RESPONSE, status=DONE``
    entry's ``content`` -- already free of the intermediate tool-call steps.
    """
    if brain_dir is None:
        brain_dir = _resolve_brain_dir(working_dir, env)
    if brain_dir is None:
        return None
    transcript = brain_dir / ".system_generated" / "logs" / "transcript.jsonl"
    try:
        raw = transcript.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None

    answer: str | None = None
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            entry = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if (
            isinstance(entry, dict)
            and entry.get("source") == "MODEL"
            and entry.get("type") == "PLANNER_RESPONSE"
            and entry.get("status") == "DONE"
        ):
            content = entry.get("content")
            if isinstance(content, str) and content.strip():
                answer = content
    return answer


def _resolve_brain_dir(working_dir: Path, env: Mapping[str, str] | None = None) -> Path | None:
    """Locate the ``brain/<conv-id>`` dir for *working_dir*'s latest turn."""
    root = _agy_state_root(env)
    brain_root = root / "brain"

    conv_id = _conv_id_for_cwd(root, working_dir)
    if conv_id:
        candidate = brain_root / conv_id
        if candidate.is_dir():
            return candidate

    return _newest_brain_dir(brain_root)


def _conv_id_for_cwd(root: Path, working_dir: Path) -> str | None:
    """Map a working directory to its conversation id via agy's cwd cache."""
    mapping_path = root / "cache" / "last_conversations.json"
    try:
        mapping = json.loads(mapping_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(mapping, dict):
        return None
    for key in (str(working_dir), os.path.realpath(working_dir)):
        conv = mapping.get(key)
        if isinstance(conv, str) and conv:
            return conv
    return None


def _newest_brain_dir(brain_root: Path) -> Path | None:
    """Return the conversation dir with the most recently written transcript."""
    try:
        candidates = [entry for entry in brain_root.iterdir() if entry.is_dir()]
    except OSError:
        return None
    best: Path | None = None
    best_mtime = -1.0
    for directory in candidates:
        transcript = directory / ".system_generated" / "logs" / "transcript.jsonl"
        try:
            mtime = transcript.stat().st_mtime
        except OSError:
            continue
        if mtime > best_mtime:
            best_mtime = mtime
            best = directory
    return best


class AntigravityLogParser(LogParser):
    """Encapsulates the agy-specific transcript.jsonl log parsing logic."""

    def is_session_active(self, session_id: str) -> bool:
        return session_id in AntigravityCLI._session_holders

    def has_active_sessions(self) -> bool:
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
                        "RUN_COMMAND": "run_command (명령어 실행)",
                        "VIEW_FILE": "view_file (파일 보기)",
                        "LIST_DIRECTORY": "list_dir (디렉토리 조회)",
                        "GREP_SEARCH": "grep_search (패턴 검색)",
                        "CODE_ACTION": "replace_file_content (파일 수정)",
                    }
                    name = friendly_names.get(etype, etype.lower())
                    tool_completions.append(f"`{name}` 완료")

        parts = []
        if thinking_blocks:
            combined_thinking = "\n\n".join(thinking_blocks)
            blockquote_thinking = "\n".join(f"> {l}" for l in combined_thinking.splitlines())
            parts.append(f"💭 **생각 흐름:**\n{blockquote_thinking}")
        if tool_calls:
            calls_list = "\n".join(f"• {tc}" for tc in tool_calls)
            parts.append(f"🛠️ **도구 호출:**\n{calls_list}")
        if tool_completions:
            completions_list = "\n".join(f"• {tc}" for tc in tool_completions)
            parts.append(f"📥 **도구 완료:**\n{completions_list}")
        if final_content:
            parts.append(f"✅ **최종 답변:**\n{final_content}")

        formatted_text = None
        if parts:
            header = "**[우덕터 백그라운드 완료 알림]**" if final_content else "**[우덕터 백그라운드 진행 상황]**"
            formatted_text = f"{header}\n\n" + "\n\n".join(parts)

        return file_size, formatted_text


def _cleanup_on_exit():
    import os
    import signal
    holders = list(AntigravityCLI._session_holders.items())
    if holders:
        for session_id, holder in holders:
            try:
                if holder.proc.poll() is None:
                    pgid = os.getpgid(holder.proc.pid)
                    if pgid != os.getpgrp():
                        os.killpg(pgid, signal.SIGKILL)
                    else:
                        holder.proc.kill()
                    holder.proc.wait(timeout=1.0)
            except Exception:
                pass
            try:
                os.close(holder.master_fd)
            except OSError:
                pass
        AntigravityCLI._session_holders.clear()

import atexit
atexit.register(_cleanup_on_exit)
