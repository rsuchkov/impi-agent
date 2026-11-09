"""MessageCoalescer: one worker per conversation, merging piled-up messages.

Without this, several messages sent while the agent is mid-turn become several
serialized turns (N replies, wasted tokens). Here, messages that arrive while a
conversation's turn is running are buffered and handed to the NEXT turn as one
batch — so a burst yields one combined prompt and one reply. With
``join_running_turns`` a message that arrives mid-turn is put to that turn
instead (``join``), for the model to read before its next call; one the turn
ends without reading is queued as the next turn, as it would have been.

Race-free by construction: submit() is synchronous (no await), and a worker has
no await between checking its conversation's buffer empty and removing itself, so
a submit can never slip in unseen.
"""

import asyncio
import logging
from collections.abc import Callable

from crucible.ports.chat.client import ChatClient
from crucible.ports.chat.flow import Flow, Joined, TurnJoiner, TurnOutcome, TurnStopper
from crucible.ports.chat.keying import ConversationKey, PlatformKey
from crucible.ports.chat.types import IncomingMessage

logger = logging.getLogger(__name__)


class MessageCoalescer:
    def __init__(
        self, flow: Flow, *, on_arrival: Callable[[IncomingMessage], object] | None = None,
        agent: str = "", keying: ConversationKey | None = None, join_running_turns: bool = False,
    ) -> None:
        self._flow = flow
        # Whether a message that arrives mid-turn is put to that turn (when the
        # flow can) rather than kept for the next one.
        self._join_running_turns = join_running_turns
        # What makes two messages one conversation — the same answer the flow
        # keeps its session by, so a burst from two gateways is one batch.
        self._agent = agent
        self._keying: ConversationKey = keying or PlatformKey()
        # Fired synchronously when a real (non-synthetic) message arrives, BEFORE
        # it queues — used to cancel an outstanding blocking UI request so a paused
        # turn unblocks (the user typed instead of clicking). Kept a plain callback
        # so the coalescer stays free of UI/store knowledge.
        self._on_arrival = on_arrival
        self._pending: dict[str, list[IncomingMessage]] = {}
        self._workers: dict[str, asyncio.Task] = {}
        # message id -> whoever is waiting for that turn's outcome.
        self._tracked: dict[str, asyncio.Future[TurnOutcome]] = {}
        self._joining: set[asyncio.Task] = set()

    async def join(self, msg: IncomingMessage, chat: ChatClient) -> Joined | None:
        """``JoinableSink``: put ``msg`` to the turn running in its conversation,
        through the flow behind. None when no turn runs there (nothing is
        queued either — the caller decides), or the flow will not take it —
        another person's message, say: who may join is the flow's check."""
        if not isinstance(self._flow, TurnJoiner):
            return None
        if self._on_arrival is not None and not msg.synthetic:
            self._on_arrival(msg)
        conv = self._keying.key(self._agent, msg)
        if conv not in self._workers:
            return None
        return await self._flow.join(msg, chat)

    async def stop(self, conversation_id: str, *, gateway: str = "") -> bool:
        """``StoppableSink``, for the flow behind: the coalescer is the sink a
        gateway holds, the flow is what runs the turn and can end it."""
        if not isinstance(self._flow, TurnStopper):
            return False
        return await self._flow.stop(conversation_id, gateway=gateway)

    def submit(self, msg: IncomingMessage, chat: ChatClient) -> None:
        if self._on_arrival is not None and not msg.synthetic:
            self._on_arrival(msg)
        conv = self._keying.key(self._agent, msg)
        if (
            self._join_running_turns and not msg.synthetic and not msg.attachments
            and conv in self._workers and isinstance(self._flow, TurnJoiner)
        ):
            # A turn is running here: the message goes to it, and comes back to
            # the queue only if the turn ends without reading it.
            task = asyncio.create_task(self._join_or_queue(conv, msg, chat))
            self._joining.add(task)
            task.add_done_callback(self._joining.discard)
            return
        self._queue(conv, msg, chat)

    def _queue(self, conv: str, msg: IncomingMessage, chat: ChatClient) -> None:
        self._pending.setdefault(conv, []).append(msg)
        if conv not in self._workers:
            self._workers[conv] = asyncio.create_task(self._run(conv, chat))

    async def _join_or_queue(self, conv: str, msg: IncomingMessage, chat: ChatClient) -> None:
        joined = await self._flow.join(msg, chat) if isinstance(self._flow, TurnJoiner) else None
        if joined is None:
            self._queue(conv, msg, chat)  # no turn to join after all, or not this person's
            return
        if not await joined.read:
            # Queued as the turn ends — after whatever the worker has already
            # taken for the next turn, since it moves on before this wakes.
            logger.info("a message joined a turn that ended before reading it; queued as the next turn")
            self._queue(conv, msg, chat)

    def submit_tracked(
        self, msg: IncomingMessage, chat: ChatClient
    ) -> asyncio.Future[TurnOutcome]:
        """Submit, and hand back the turn's outcome when it is known — for turns
        the engine starts itself and has to report on. It queues like anything
        else; the point is to learn what happened, not to skip the batching.

        A batch resolves every waiter in it with the SAME outcome: if a person
        wrote into the conversation while a scheduled turn was queued, the two
        messages become one turn, and one turn has one ending."""
        future: asyncio.Future[TurnOutcome] = asyncio.get_running_loop().create_future()
        # Registered before the submit that may start the worker, so an outcome
        # can never be produced before there is somewhere to put it.
        self._tracked[msg.ref.message_id] = future
        self.submit(msg, chat)
        return future

    async def _run(self, conv: str, chat: ChatClient) -> None:
        try:
            while self._pending.get(conv):
                batch = self._pending.pop(conv)
                try:
                    outcome = await self._flow.handle_batch(batch, chat)
                except asyncio.CancelledError:
                    self._abandon(batch)  # shutdown: nobody will ever answer
                    raise
                except Exception:
                    # The flow reports its own user-facing errors; this is the
                    # backstop for bugs so one bad turn doesn't wedge the worker.
                    logger.exception("flow crashed on a batch of %d in %s", len(batch), conv)
                    self._settle(batch, TurnOutcome.ERROR)
                else:
                    self._settle(batch, outcome)
        finally:
            self._workers.pop(conv, None)
            # Anything queued but never batched (the worker was cancelled between
            # turns) has no turn coming; waiting on it forever is worse than
            # knowing it was dropped.
            self._abandon(self._pending.get(conv, []))

    def _settle(self, batch: list[IncomingMessage], outcome: TurnOutcome) -> None:
        for msg in batch:
            future = self._tracked.pop(msg.ref.message_id, None)
            if future is not None and not future.done():
                future.set_result(outcome)

    def _abandon(self, batch: list[IncomingMessage]) -> None:
        for msg in batch:
            future = self._tracked.pop(msg.ref.message_id, None)
            if future is not None and not future.done():
                future.cancel()
