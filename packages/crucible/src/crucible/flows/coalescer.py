"""MessageCoalescer: one worker per conversation, merging piled-up messages.

Without this, several messages sent while the agent is mid-turn become several
serialized turns (N replies, wasted tokens). Here, messages that arrive while a
conversation's turn is running are buffered and handed to the NEXT turn as one
batch — so a burst yields one combined prompt and one reply.

Race-free by construction: submit() is synchronous (no await), and a worker has
no await between checking its conversation's buffer empty and removing itself, so
a submit can never slip in unseen.
"""

import asyncio
import logging
from collections.abc import Callable

from crucible.ports.chat.client import ChatClient
from crucible.ports.chat.flow import Flow
from crucible.ports.chat.types import IncomingMessage

logger = logging.getLogger(__name__)


class MessageCoalescer:
    def __init__(
        self, flow: Flow, *, on_arrival: Callable[[IncomingMessage], object] | None = None
    ) -> None:
        self._flow = flow
        # Fired synchronously when a real (non-synthetic) message arrives, BEFORE
        # it queues — used to cancel an outstanding blocking UI request so a paused
        # turn unblocks (the user typed instead of clicking). Kept a plain callback
        # so the coalescer stays free of UI/store knowledge.
        self._on_arrival = on_arrival
        self._pending: dict[str, list[IncomingMessage]] = {}
        self._workers: dict[str, asyncio.Task] = {}

    def submit(self, msg: IncomingMessage, chat: ChatClient) -> None:
        if self._on_arrival is not None and not msg.synthetic:
            self._on_arrival(msg)
        conv = msg.conversation_id
        self._pending.setdefault(conv, []).append(msg)
        if conv not in self._workers:
            self._workers[conv] = asyncio.create_task(self._run(conv, chat))

    async def _run(self, conv: str, chat: ChatClient) -> None:
        try:
            while self._pending.get(conv):
                batch = self._pending.pop(conv)
                try:
                    await self._flow.handle_batch(batch, chat)
                except Exception:
                    # The flow reports its own user-facing errors; this is the
                    # backstop for bugs so one bad turn doesn't wedge the worker.
                    logger.exception("flow crashed on a batch of %d in %s", len(batch), conv)
        finally:
            self._workers.pop(conv, None)
