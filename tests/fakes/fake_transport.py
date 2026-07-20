"""In-process fake of :class:`crucible.runtimes.pi.transport.PiTransport` for unit tests.

A test pushes scripted stdout lines via :meth:`emit` (or registers a reactor
that responds to commands the session sends) and inspects :attr:`sent` to assert
what the session wrote to stdin.
"""

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable


class FakeTransport:
    Reactor = Callable[[dict], Awaitable[None] | None]

    def __init__(self, reactor: "FakeTransport.Reactor | None" = None) -> None:
        self.sent: list[dict] = []
        self._queue: asyncio.Queue[str | None] = asyncio.Queue()
        self._reactor = reactor
        self.closed = False

    async def send(self, line: str) -> None:
        if self.closed:
            raise BrokenPipeError("transport closed")
        command = json.loads(line)
        self.sent.append(command)
        if self._reactor is not None:
            result = self._reactor(command)
            if asyncio.iscoroutine(result):
                await result

    async def lines(self) -> AsyncIterator[str]:
        while True:
            item = await self._queue.get()
            if item is None:  # EOF sentinel
                return
            yield item

    async def aclose(self) -> None:
        self.closed = True
        await self._queue.put(None)

    # -- test helpers -------------------------------------------------------

    def emit(self, event: dict) -> None:
        """Queue one stdout event line for the session to read."""
        self._queue.put_nowait(json.dumps(event) + "\n")

    def emit_raw(self, line: str) -> None:
        self._queue.put_nowait(line)

    def eof(self) -> None:
        """Simulate the process exiting (stdout EOF)."""
        self._queue.put_nowait(None)

    def last_sent(self) -> dict:
        return self.sent[-1]

    def sent_types(self) -> list[str | None]:
        return [command.get("type") for command in self.sent]
