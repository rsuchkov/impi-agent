"""What one turn's tool calls were — the record every gateway's view of a turn
is drawn from, and the store that keeps it.

A turn's calls are the same facts wherever the turn is watched: the widget
under a chat reply, the journal an HTTP caller reads, a platform's own view of
the turn. ``TurnCalls`` collects them from the runtime's events as the turn
runs; ``TraceRecord`` is what is written once when the turn ends, so the calls
can be read minutes or a restart later; ``Traces`` is what writes and reads it,
which the store implements.

Arguments are kept; results never are. The engine never holds a value the
agent was granted (a secret travels by reference and is bound into a process
the model does not read), so arguments cannot carry one — a result could, and
no view of a turn is a place a value may surface.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import Protocol

from crucible.ports.agent.events import (
    CallAwaiting,
    TextProgress,
    ToolFinished,
    ToolStarted,
)

# Arguments as kept in the record. Larger than anyone reads in chat because a
# `write` call carries a whole file; whoever shows them cuts at display time.
ARGS_STORED_MAX = 8000

CALL_OK = "ok"
CALL_FAILED = "failed"
CALL_UNFINISHED = "unfinished"


@dataclass
class Call:
    """One tool call as the record keeps it. ``args`` is compact JSON, cut at
    ``ARGS_STORED_MAX`` with the original length in ``cut`` when it was."""

    index: int
    call_id: str
    tool: str
    args: str = "{}"
    cut: int = 0
    state: str = CALL_UNFINISHED
    duration_s: float = 0.0
    caption: str = ""  # the call's one line for a person, when whoever knows the tool gave one


def encode_calls(calls: list[Call]) -> str:
    return json.dumps([asdict(call) for call in calls], ensure_ascii=False)


def decode_calls(raw: str) -> list[Call]:
    try:
        return [Call(**item) for item in json.loads(raw)]
    except (json.JSONDecodeError, TypeError, ValueError):
        return []


class TurnCalls:
    """The calls of one turn, collected from the runtime's events as they come.

    ``latest`` is the latest word for a person watching — a call's caption, or
    the model's remark on its way to the next call — kept nowhere but here: the
    record is the calls. ``awaiting`` is the call put to the person (a card is
    up), until it finishes. ``on_event`` says whether the event changed any of
    this, so a view redraws on a call and not on a remark before any."""

    def __init__(self) -> None:
        self._calls: list[Call] = []
        self._open: dict[str, int] = {}  # call id -> index of the call still running
        self._awaiting: int | None = None  # index of the call waiting on the person
        self.latest = ""

    @property
    def awaiting(self) -> Call | None:
        return self._calls[self._awaiting] if self._awaiting is not None else None

    @property
    def calls(self) -> list[Call]:
        return list(self._calls)

    def __len__(self) -> int:
        return len(self._calls)

    @property
    def failed(self) -> int:
        return sum(1 for call in self._calls if call.state == CALL_FAILED)

    def on_event(self, event: object) -> bool:
        if isinstance(event, ToolStarted):
            args = json.dumps(event.args, ensure_ascii=False, separators=(",", ":"))
            cut = len(args) if len(args) > ARGS_STORED_MAX else 0
            call = Call(
                index=len(self._calls), call_id=event.call_id, tool=event.tool,
                args=args[:ARGS_STORED_MAX], cut=cut, caption=event.caption,
            )
            self._calls.append(call)
            self._open[event.call_id] = call.index
            if event.caption:
                self.latest = event.caption
        elif isinstance(event, TextProgress):
            if event.text.strip():
                self.latest = event.text
        elif isinstance(event, ToolFinished):
            index = self._open.pop(event.call_id, None)
            if index is None:
                return False
            call = self._calls[index]
            call.state = CALL_FAILED if event.is_error else CALL_OK
            call.duration_s = event.duration_s
            if self._awaiting == index:
                self._awaiting = None
        elif isinstance(event, CallAwaiting):
            # The gate names the tool, not the call: the one of that tool still
            # running is the one it asked about (a runtime runs calls one at a time).
            waiting = [i for i in self._open.values() if self._calls[i].tool == event.tool]
            if not waiting:
                return False
            self._awaiting = waiting[-1]
        else:
            return False
        return bool(self._calls)


@dataclass(frozen=True)
class TraceRecord:
    """What one turn's tool calls were, kept so a view of the turn can be
    opened long after it — minutes or a restart later.

    ``calls`` is opaque JSON (``encode_calls``), like a form's ``spec``: the
    record owns its shape and the store only keeps it. It holds names,
    arguments, outcomes and timings; never results. ``post_id`` names the
    widget message the trace is drawn in, "" when it is drawn in none.
    """

    token: str
    agent: str
    channel_id: str
    conversation_id: str
    kind: str
    created_at: str
    finished_at: str
    calls: str
    post_id: str = ""


class Traces(Protocol):
    """One record per turn that called tools, written once when the turn ends
    and read on every look."""

    async def create_trace(self, record: TraceRecord) -> None: ...

    async def get_trace(self, token: str) -> TraceRecord | None: ...
