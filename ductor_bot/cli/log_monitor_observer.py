import asyncio
import contextlib
import logging
from pathlib import Path
from ductor_bot.bus.bus import MessageBus
from ductor_bot.bus.envelope import Envelope, Origin, LockMode
from ductor_bot.cli.factory import create_cli
from ductor_bot.cli.base import BaseCLI, CLIConfig
from ductor_bot.workspace.paths import DuctorPaths

logger = logging.getLogger(__name__)

class LogMonitorObserver:
    """Background task that periodically scans active conversation logs
    and pushes new MODEL responses to the MessageBus.
    """

    def __init__(self, paths: DuctorPaths, orchestrator) -> None:
        self._paths = paths
        self._orchestrator = orchestrator
        self._bus: MessageBus | None = None
        self._task: asyncio.Task[None] | None = None
        self._last_sizes: dict[str, int] = {}
        self._cli_cache: dict[str, BaseCLI] = {}

    def wire_to_bus(self, bus: MessageBus) -> None:
        self._bus = bus

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run_loop())
            logger.info("LogMonitorObserver started")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
            logger.info("LogMonitorObserver stopped")

    async def _run_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(5.0)
                if self._orchestrator is None or self._bus is None:
                    continue

                sessions = await self._orchestrator._sessions.list_all()
                for session in sessions:
                    chat_id = session.chat_id
                    topic_id = session.topic_id
                    transport = getattr(session, "transport", "tg")

                    provider_name = session.provider
                    provider_data = session.provider_sessions.get(provider_name)
                    if not provider_data or not provider_data.session_id:
                        continue

                    session_id = provider_data.session_id

                    # Get or create CLI instance from cache to avoid redundant allocations and logs
                    cli_instance = self._cli_cache.get(provider_name)
                    if cli_instance is None:
                        cli_config = CLIConfig(
                            provider=provider_name,
                            working_dir=self._orchestrator.config.working_dir,
                        )
                        cli_instance = create_cli(cli_config)
                        self._cli_cache[provider_name] = cli_instance

                    parser = cli_instance.get_log_parser()
                    if parser is None:
                        continue

                    if not parser.is_session_active(session_id):
                        continue

                    transcript_path = parser.get_transcript_path(session_id)
                    if not transcript_path.is_file():
                        continue

                    prev_size = self._last_sizes.get(str(transcript_path))
                    new_size, formatted_text = parser.parse_log_delta(
                        session_id, transcript_path, prev_size
                    )

                    if new_size is not None:
                        self._last_sizes[str(transcript_path)] = new_size

                    if formatted_text:
                        logger.info(
                            "LogMonitorObserver: Submitting log delta to bus for chat %s topic %s",
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
                        await self._bus.submit(envelope)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Error in LogMonitorObserver loop: %s", e)
