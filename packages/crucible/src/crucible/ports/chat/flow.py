"""Flow port: consumes a neutral inbound message, replies via ChatClient."""

from collections.abc import Awaitable
from enum import Enum, auto
from typing import Protocol

from crucible.ports.chat.client import ChatClient
from crucible.ports.chat.types import IncomingMessage


class TurnOutcome(Enum):
    """How a turn ended.

    A gateway ignores this — it posts a message and moves on. It exists for
    callers that started the turn themselves and have to report on it (a
    scheduled task saying whether its run worked), because "the flow returned"
    and "the agent answered" are not the same thing.

    The flow has already told the user about TIMEOUT, ERROR and EMPTY by the
    time it returns them; a caller that reports failures must not say it twice.
    """

    REPLIED = auto()  # the agent produced text and it was posted
    ACTED = auto()  # no text, but tools ran — a posted widget IS the reply
    EMPTY = auto()  # neither: the user was nudged to rephrase
    TIMEOUT = auto()  # the runtime timed out; the fallback message was posted
    ERROR = auto()  # the runtime failed; the internal-error message was posted
    DUPLICATE = auto()  # every message in the batch had already been processed


class Flow(Protocol):
    """A conversation flow. Holds no platform knowledge. ``handle_batch`` is the
    entry point (a single message is a batch of one); the coalescer merges
    messages of one conversation that pile up during a turn into one batch."""

    async def handle_batch(
        self, msgs: list[IncomingMessage], chat: ChatClient
    ) -> TurnOutcome: ...


class MessageSink(Protocol):
    """Where a gateway hands each message it decides to act on. Fire-and-forget
    (the sink owns concurrency). A gateway depends on this, not on flows."""

    def submit(self, msg: IncomingMessage, chat: ChatClient) -> None: ...


class TrackedSink(MessageSink, Protocol):
    """A sink that can also say how the turn it started ended.

    For the engine's own turns: a gateway keeps using ``submit`` and never waits,
    while something that scheduled a turn awaits its outcome — without either
    bypassing the queueing the sink exists to provide."""

    def submit_tracked(
        self, msg: IncomingMessage, chat: ChatClient
    ) -> Awaitable[TurnOutcome]: ...
