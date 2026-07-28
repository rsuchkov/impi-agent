"""Transport abstraction between :class:`PiRpcSession` and a pi process.

Splitting the byte/stream plumbing from the protocol logic lets tests drive a
``PiRpcSession`` with a scripted fake instead of spawning a real subprocess.
"""

import asyncio
import collections
import logging
from typing import AsyncIterator, Protocol, runtime_checkable

logger = logging.getLogger(__name__)


def _stderr_log_level(text: str) -> int:
    """Pick the log level for a line of pi stderr.

    pi takes an advisory lock on project settings at startup
    (``settings.json.lock``). It can fail on read-only profile dirs (EROFS /
    EACCES) and shrugs it off — it reads the settings fine and continues. We
    serialize turns per conversation ourselves, so the lock is moot. Keep these
    out of WARNING; everything else stays WARNING so real problems surface.
    """
    if "settings.json.lock" in text:
        return logging.DEBUG
    return logging.WARNING


@runtime_checkable
class PiTransport(Protocol):
    """A bidirectional line channel to a pi RPC process."""

    async def send(self, line: str) -> None:
        """Write one already-newline-terminated JSONL command to stdin."""

    def lines(self) -> AsyncIterator[str]:
        """Yield stdout lines until EOF (process exit)."""
        ...

    async def aclose(self) -> None:
        """Terminate the process / release resources. Idempotent."""


class SubprocessTransport:
    """Drives a real ``pi --mode rpc`` subprocess via asyncio pipes."""

    def __init__(self, process: asyncio.subprocess.Process) -> None:
        self._process = process
        self._stderr_tail: collections.deque[str] = collections.deque(
            maxlen=self._STDERR_TAIL_LINES
        )
        self._stderr_task: asyncio.Task[None] | None = None
        if process.stderr is not None:
            self._stderr_task = asyncio.ensure_future(self._drain_stderr(process.stderr))

    # pi emits one JSON event per stdout line; a single event (large tool result
    # or message) can exceed asyncio's default 64 KiB StreamReader buffer, which
    # would raise LimitOverrunError and crash the read loop. Give the reader room.
    _STREAM_LIMIT = 16 * 1024 * 1024
    # The retained stderr tail: enough to carry the actual crash cause into a
    # process-death error without unbounded growth.
    _STDERR_TAIL_LINES = 40
    _STDERR_TAIL_CHARS = 1500

    @classmethod
    async def spawn(
        cls,
        program: str,
        args: list[str],
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
    ) -> "SubprocessTransport":
        process = await asyncio.create_subprocess_exec(
            program,
            *args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
            env=env,
            limit=cls._STREAM_LIMIT,
        )
        return cls(process)

    async def send(self, line: str) -> None:
        if self._process.stdin is None:
            raise BrokenPipeError("pi process stdin is closed")
        self._process.stdin.write(line.encode("utf-8"))
        await self._process.stdin.drain()

    async def lines(self) -> AsyncIterator[str]:
        assert self._process.stdout is not None
        while True:
            raw = await self._process.stdout.readline()
            if not raw:  # EOF -> process closed stdout / exited
                break
            yield raw.decode("utf-8", errors="replace")

    async def exit_detail(self) -> str:
        """Exit code + retained stderr tail for a process-death error message —
        the tail carries the actual cause (bad model config, invalid URL, ...)
        that a bare "exited unexpectedly" hides. Waits briefly so the exit code
        and the final stderr lines settle; empty when nothing is known."""
        try:
            await asyncio.wait_for(self._process.wait(), timeout=2.0)
        except (asyncio.TimeoutError, ProcessLookupError):
            pass
        if self._stderr_task is not None:
            try:
                await asyncio.wait_for(asyncio.shield(self._stderr_task), timeout=0.5)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                pass
        parts = []
        if self._process.returncode is not None:
            parts.append(f"exit code {self._process.returncode}")
        tail = "\n".join(self._stderr_tail)[-self._STDERR_TAIL_CHARS:]
        if tail:
            parts.append(f"last stderr: {tail}")
        return "; ".join(parts)

    async def aclose(self) -> None:
        process = self._process
        if process.returncode is None:
            try:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), timeout=5.0)
                except asyncio.TimeoutError:
                    process.kill()
                    await process.wait()
            except ProcessLookupError:
                pass
        if self._stderr_task is not None:
            self._stderr_task.cancel()

    async def _drain_stderr(self, stderr: asyncio.StreamReader) -> None:
        # stderr must be drained continuously or the pipe buffer fills and the
        # child deadlocks. We forward it to the log.
        try:
            while True:
                raw = await stderr.readline()
                if not raw:
                    break
                text = raw.decode("utf-8", errors="replace").rstrip()
                if text:
                    level = _stderr_log_level(text)
                    logger.log(level, "pi stderr: %s", text)
                    if level >= logging.WARNING:  # keep lock noise out of the tail
                        self._stderr_tail.append(text)
        except asyncio.CancelledError:
            pass
