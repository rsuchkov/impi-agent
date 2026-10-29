"""Flow port: consumes a neutral inbound message, replies via ChatClient."""

from collections.abc import Awaitable
from enum import Enum, auto
from typing import Protocol, runtime_checkable

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
    ACTED = auto()  # no text, but a tool that posts to the person ran — its post IS the reply
    EMPTY = auto()  # no text and no such call (nothing, or only lookups): the user got a notice
    # The answer hit the model's length limit: what came out, if anything, was
    # posted, then a notice saying so. Not a failure of the engine's.
    TRUNCATED = auto()
    TIMEOUT = auto()  # the turn ran past its limit, stalled, or the engine was full; a notice said which
    ERROR = auto()  # the runtime failed; the internal-error message was posted
    DUPLICATE = auto()  # every message in the batch had already been processed
    # A person stopped the turn (a command, a button, a cancel frame): what it
    # had was posted, then a notice saying so. The conversation keeps its memory.
    STOPPED = auto()


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


@runtime_checkable
class TrackedSink(MessageSink, Protocol):
    """A sink that can also say how the turn it started ended.

    For the engine's own turns: a gateway keeps using ``submit`` and never waits,
    while something that scheduled a turn awaits its outcome — without either
    bypassing the queueing the sink exists to provide."""

    def submit_tracked(
        self, msg: IncomingMessage, chat: ChatClient
    ) -> Awaitable[TurnOutcome]: ...


@runtime_checkable
class TurnStopper(Protocol):
    """What can end a turn in flight on a person's word — the flow itself, or
    the sink in front of it (``StoppableSink``) handing the word on."""

    async def stop(self, conversation_id: str, *, gateway: str = "") -> bool: ...


@runtime_checkable
class StoppableSink(MessageSink, TurnStopper, Protocol):
    """A sink whose turn in flight a person can stop.

    ``conversation_id`` is the conversation as the sink keys it — or, with
    ``gateway``, as that gateway names it (the platform's id), which is what a
    slash command or a reaction knows; the sink does the keying. True when a
    turn was running there and was told to end; False when nothing was. The
    turn then ends with what it had, says so in the conversation, and the next
    one is told it was stopped — all the sink's doing, whoever asked.

    Two words, one rule: *stop* is the person's act and the turn's state
    (``STOP_COMMAND``, ``TurnOutcome.STOPPED``, the ``stopped`` notice);
    *cancel* is a program's word to a program (``RuntimeControl.cancel``, the
    http ``/cancel`` route, the ws ``cancel`` frame)."""
