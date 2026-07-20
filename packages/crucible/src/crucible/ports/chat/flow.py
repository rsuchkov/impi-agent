"""Flow port: consumes a neutral inbound message, replies via ChatClient."""

from typing import Protocol

from crucible.ports.chat.client import ChatClient
from crucible.ports.chat.types import IncomingMessage


class Flow(Protocol):
    """A conversation flow. Holds no platform knowledge. ``handle_batch`` is the
    entry point (a single message is a batch of one); the coalescer merges
    messages of one conversation that pile up during a turn into one batch."""

    async def handle_batch(
        self, msgs: list[IncomingMessage], chat: ChatClient
    ) -> None: ...


class MessageSink(Protocol):
    """Where a gateway hands each message it decides to act on. Fire-and-forget
    (the sink owns concurrency). A gateway depends on this, not on flows."""

    def submit(self, msg: IncomingMessage, chat: ChatClient) -> None: ...
