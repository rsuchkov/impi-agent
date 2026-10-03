"""TurnJournal: the event log of one turn, read by a client that polls.

A chat platform shows a reply where it lands; a program calling over HTTP has
to come back and ask. So everything a turn produces — the tools it ran, the
cards it put up, its answer, how it ended — is appended here with a sequence
number, and the client reads from where it left off. The journal outlives the
turn by a retention window so a client that was away (a page reload) can
still catch up.
"""

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

STATUS_RUNNING = "running"
STATUS_AWAITING_INPUT = "awaiting_input"  # a card is up: the turn waits on the person
STATUS_DONE = "done"
STATUS_FAILED = "failed"

EV_TURN_STARTED = "turn.started"
EV_TOOL_STARTED = "tool.started"
EV_TOOL_FINISHED = "tool.finished"
EV_MESSAGE = "message"
EV_NOTICE = "notice"
EV_ACTIONS = "actions"  # a message with controls (a confirmation card, a question)
EV_ACTIONS_RETIRED = "actions.retired"  # its controls are gone; `text` replaces it
EV_CARDS = "cards"  # a message built from cards; the same postId again = redraw it
EV_FILE = "file"
EV_TURN_FINISHED = "turn.finished"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass(frozen=True)
class TurnEvent:
    seq: int
    at: str
    type: str
    payload: dict[str, Any] = field(default_factory=dict)

    def to_wire(self) -> dict[str, Any]:
        return {"seq": self.seq, "at": self.at, "type": self.type, **self.payload}


class TurnJournal:
    def __init__(self, turn_id: str) -> None:
        self.turn_id = turn_id
        self.status = STATUS_RUNNING
        self.finished_at: float | None = None  # loop time, for retention
        self._events: list[TurnEvent] = []
        # Replaced on every change: a waiter holds the Event that was current
        # when it started, so a change it has not seen yet always wakes it.
        self._changed = asyncio.Event()

    @property
    def cursor(self) -> int:
        return self._events[-1].seq if self._events else 0

    @property
    def finished(self) -> bool:
        return self.finished_at is not None

    def emit(self, event_type: str, **payload: Any) -> TurnEvent:
        event = TurnEvent(seq=self.cursor + 1, at=_now(), type=event_type, payload=payload)
        self._events.append(event)
        self._wake()
        return event

    def set_status(self, status: str) -> None:
        if not self.finished:
            self.status = status
            self._wake()

    def finish(self, status: str) -> None:
        self.status = status
        self.finished_at = asyncio.get_running_loop().time()
        self._wake()

    def since(self, after: int) -> list[TurnEvent]:
        return [e for e in self._events if e.seq > after]

    async def wait(self, after: int, max_wait_s: float) -> list[TurnEvent]:
        """Events newer than ``after``; waits up to ``max_wait_s`` for a change
        when there are none yet and the turn is still going."""
        found = self.since(after)
        if found or max_wait_s <= 0 or self.finished:
            return found
        changed = self._changed
        try:
            await asyncio.wait_for(changed.wait(), max_wait_s)
        except asyncio.TimeoutError:
            return []
        return self.since(after)

    def _wake(self) -> None:
        self._changed.set()
        self._changed = asyncio.Event()
