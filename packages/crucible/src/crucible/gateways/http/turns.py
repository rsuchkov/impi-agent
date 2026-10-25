"""HttpTurns: the turns in flight on the HTTP gateway, and the journals a
client reads them from.

One running turn per conversation — a second message while one runs is refused
with the running turn's id, so the client follows it instead of the engine
quietly batching two requests into one answer. A repeated client message id
returns the turn it already started, so a retried request is not a second
turn. The turn's journal stays readable for a retention window after it ends.

Also the flow's ``TurnTracer``: the runtime's tool events for a turn go into
that turn's journal, which is how the client gets "searching…" while it waits.

And the confirmation cards a conversation holds outside any turn of this
gateway's: a card raised by a turn running on another gateway is forwarded
here as to every gateway the conversation is live in, and a client with no
turn to follow asks the conversation what it is waiting on.
"""

import asyncio
import logging
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field

from crucible.gateways.http.journal import (
    EV_PROGRESS,
    EV_TOOL_FINISHED,
    EV_TOOL_STARTED,
    EV_TURN_FINISHED,
    EV_TURN_STARTED,
    STATUS_DONE,
    STATUS_FAILED,
    TurnJournal,
)
from crucible.ports.agent.events import TextProgress, ToolFinished, ToolStarted
from crucible.ports.chat.flow import TurnOutcome
from crucible.ports.chat.types import Action, ApprovalCard

logger = logging.getLogger(__name__)

# Who opened a turn, on its first journal event: the caller's message, or the
# engine by itself (a scheduled task, an event the application reported).
STARTED_BY_CALLER = "caller"
STARTED_BY_ENGINE = "engine"

# Which outcomes count as the turn having worked. The names travel to the
# client lower-cased (`replied`, `timeout`, …) and are part of the contract.
_FAILED = frozenset({TurnOutcome.TIMEOUT, TurnOutcome.ERROR})


class TurnInProgress(Exception):
    """The conversation already has a turn running; carries its id."""

    def __init__(self, turn_id: str) -> None:
        super().__init__(f"a turn is already running: {turn_id}")
        self.turn_id = turn_id


@dataclass(eq=False)
class Turn:
    id: str
    agent: str
    conversation: str  # the engine-side key (realm-namespaced)
    owner: str  # who may read it: the caller's realm/user
    client_message_id: str
    journal: TurnJournal
    started_by: str = STARTED_BY_CALLER
    runtime_session_id: str = ""  # learned when the flow begins the turn
    # The controls this turn has put up, by post id, so a click can be matched
    # to the action it names — and the token in that action's context. Cleared
    # when the turn ends: a card left over from a finished turn answers nobody.
    posted: dict[str, tuple[Action, ...]] = field(default_factory=dict)
    # The posts the turn is waiting on (a confirmation card), as opposed to a
    # screen it merely drew: the status says "awaiting input" while any is up.
    blocking: set[str] = field(default_factory=set)


class _Trace:
    """The flow's view of a turn: feed it the runtime's events."""

    def __init__(self, turn: Turn | None) -> None:
        self._turn = turn

    def on_event(self, event: object) -> None:
        if self._turn is None:
            return
        if isinstance(event, ToolStarted):
            self._turn.journal.emit(
                EV_TOOL_STARTED, callId=event.call_id, tool=event.tool, caption=event.caption
            )
        elif isinstance(event, TextProgress):
            self._turn.journal.emit(EV_PROGRESS, text=event.text)
        elif isinstance(event, ToolFinished):
            self._turn.journal.emit(
                EV_TOOL_FINISHED, callId=event.call_id, tool=event.tool, ok=not event.is_error
            )

    async def end(self, *, interrupted: bool = False) -> None:
        return  # the ending is reported from the outcome, not from here


@dataclass(eq=False)
class PendingCard:
    """A confirmation card a conversation holds outside any turn of this
    gateway's, until its verdict arrives."""

    post_id: str
    card: ApprovalCard
    actions: tuple[Action, ...]


# How many cards one conversation holds outside a turn; the oldest goes first.
PENDING_CARDS_PER_CONVERSATION = 50


class HttpTurns:
    def __init__(self, *, journal_retention_s: float = 600.0, remembered: int = 10_000) -> None:
        self._retention = journal_retention_s
        self._remembered = remembered
        self._by_id: dict[str, Turn] = {}
        self._active: dict[str, Turn] = {}  # conversation -> its running turn
        self._pending: dict[str, dict[str, PendingCard]] = {}  # conversation -> post id -> card
        # Set while nothing runs; what a hub draining before shutdown waits on.
        self.idle = asyncio.Event()
        self.idle.set()
        # (conversation, client message id) -> turn id, bounded: a retry of a
        # message from a while ago is the same turn, one from an hour ago is new.
        self._by_message: OrderedDict[tuple[str, str], str] = OrderedDict()

    # -- starting and ending ---------------------------------------------------

    def start(
        self, *, agent: str, conversation: str, owner: str, client_message_id: str,
        started_by: str = STARTED_BY_CALLER,
    ) -> tuple[Turn, bool]:
        """A new turn for the conversation, or the one this message already
        started (``created`` False). Raises TurnInProgress when another message
        of the conversation is still being answered."""
        known = self._by_message.get((conversation, client_message_id))
        if known is not None and known in self._by_id:
            return self._by_id[known], False
        running = self._active.get(conversation)
        if running is not None:
            raise TurnInProgress(running.id)
        turn_id = uuid.uuid4().hex
        turn = Turn(
            id=turn_id, agent=agent, conversation=conversation, owner=owner,
            client_message_id=client_message_id, journal=TurnJournal(turn_id),
            started_by=started_by,
        )
        self._by_id[turn_id] = turn
        self._active[conversation] = turn
        self.idle.clear()
        self._by_message[(conversation, client_message_id)] = turn_id
        while len(self._by_message) > self._remembered:
            self._by_message.popitem(last=False)
        turn.journal.emit(EV_TURN_STARTED, startedBy=started_by)
        return turn, True

    def finish(self, turn: Turn, outcome: TurnOutcome) -> None:
        turn.posted.clear()
        turn.blocking.clear()
        turn.journal.emit(EV_TURN_FINISHED, outcome=outcome.name.lower())
        turn.journal.finish(STATUS_FAILED if outcome in _FAILED else STATUS_DONE)
        if self._active.get(turn.conversation) is turn:
            del self._active[turn.conversation]
        if not self._active:
            self.idle.set()

    def abandon(self, turn: Turn) -> None:
        """The turn never ran (the sink refused, or the engine is stopping)."""
        self.finish(turn, TurnOutcome.ERROR)

    # -- looking up --------------------------------------------------------------

    def known(self, conversation: str, client_message_id: str) -> Turn | None:
        """The turn this message already started, if it is still remembered."""
        turn_id = self._by_message.get((conversation, client_message_id))
        return self._by_id.get(turn_id) if turn_id is not None else None

    def get(self, turn_id: str) -> Turn | None:
        return self._by_id.get(turn_id)

    def active(self, conversation: str) -> Turn | None:
        return self._active.get(conversation)

    def active_count(self) -> int:
        return len(self._active)

    def posted_in(self):
        """The running turns — where a post id can still be acted on."""
        return list(self._active.values())

    # -- cards outside a turn ---------------------------------------------------

    def hold(self, conversation: str, post_id: str, card: ApprovalCard, actions: tuple[Action, ...]) -> None:
        """A card the conversation holds with no turn of this gateway's to
        put it in; replaces the same post when it is rewritten."""
        held = self._pending.setdefault(conversation, {})
        held[post_id] = PendingCard(post_id, card, actions)
        while len(held) > PENDING_CARDS_PER_CONVERSATION:
            del held[next(iter(held))]

    def pending(self, conversation: str) -> list[PendingCard]:
        """The cards the conversation holds, oldest first."""
        return list(self._pending.get(conversation, {}).values())

    def conversation_of(self, post_id: str) -> str | None:
        """The conversation holding this post, or None."""
        return next((conversation for conversation, held in self._pending.items() if post_id in held), None)

    def release(self, post_id: str) -> None:
        """The card's question is over: the verdict came, or it was withdrawn."""
        for conversation, held in list(self._pending.items()):
            if held.pop(post_id, None) is not None and not held:
                del self._pending[conversation]

    def sweep(self, now: float) -> int:
        """Forget finished turns whose journals nobody should poll any more."""
        stale = [
            turn_id
            for turn_id, turn in self._by_id.items()
            if turn.journal.finished_at is not None
            and now - turn.journal.finished_at > self._retention
        ]
        for turn_id in stale:
            del self._by_id[turn_id]
        return len(stale)

    # -- TurnTracer: the flow hands us the runtime's events -----------------------

    def begin(self, record, chat, *, anchor=None) -> _Trace:
        """Called by the flow as the turn starts, with the conversation's
        session record — which is where the turn learns its runtime session id
        (what a cancel needs). The journal has no threads: ``anchor`` is moot."""
        turn = self._active.get(record.conversation_id)
        if turn is not None:
            turn.runtime_session_id = record.runtime_session_id
        return _Trace(turn)
