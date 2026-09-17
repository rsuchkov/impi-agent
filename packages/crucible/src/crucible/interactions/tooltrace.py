"""The message under a reply that says what tools the turn ran.

Three states of one message. While the turn runs it is a counter — ``Running
3 tools…`` — so a pause reads as work rather than silence. When the turn ends it
becomes a button, ``Running 3 tools →``. A click redraws it into a list of the
calls, one row each, where one row at a time can be opened to show its
arguments; ``Close`` puts the button back. No tool calls, no message at all.

It is a screen: the engine posts it, the engine answers every click, no model is
involved. What a click carries is a token and a view position — the calls
themselves are in the store, written once when the turn ends, because the
message is opened long after the turn and possibly after a restart.

Arguments are shown; results never are. The engine never holds a value the
agent was granted (a secret travels by reference and is bound into a process
the model does not read), so arguments cannot carry one — a result could, and
this widget is not a place a value may surface.
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from crucible.containment import preformatted
from crucible.interactions.screens import ScreenState, View, screen_action
from crucible.interactions.service import conversation_ref
from crucible.ports.agent.events import ToolFinished, ToolStarted
from crucible.ports.chat.client import ChatClient
from crucible.ports.chat.types import Action, Card, ConversationRef
from crucible.store import clock
from crucible.store.base import SessionRecord, TraceRecord, TraceStore

logger = logging.getLogger(__name__)

# The trigger word the screen registry routes clicks by. Hyphenated so it cannot
# collide with a word a workspace has bound to an agent.
TRACE_SCREEN = "tool-trace"

# Rows per page. The whole list is one card, so the bound is the message's own
# size and the row of buttons under it (Slack allows 25 in one block).
PAGE_SIZE = 10
# One card's text. Slack cuts a section at 3000 and says nothing when the
# update is refused, so the widget stays under it with room to spare.
CARD_MAX = 2800
# Arguments as shown inside the open row, and as kept in the store. The
# stored copy is larger than anyone reads in chat because a `write` call
# carries a whole file, and cutting it at display time keeps the row honest.
ARGS_SHOWN_MAX = 1200
ARGS_STORED_MAX = 8000
# How often the counter is redrawn while the turn runs. The reader loop only
# notes events; the chat platform is spoken to at most this often.
REDRAW_INTERVAL_S = 1.0

CALL_OK = "ok"
CALL_FAILED = "failed"
CALL_UNFINISHED = "unfinished"

_EXPIRED = "This trace has expired."
_BY_HAND = "This panel opens itself under an answer and cannot be opened by hand."


@dataclass
class Call:
    """One tool call as the widget keeps it. ``args`` is compact JSON, cut at
    ``ARGS_STORED_MAX`` with the original length in ``cut`` when it was."""

    index: int
    call_id: str
    tool: str
    args: str = "{}"
    cut: int = 0
    state: str = CALL_UNFINISHED
    duration_s: float = 0.0


def encode_calls(calls: list[Call]) -> str:
    return json.dumps([asdict(call) for call in calls], ensure_ascii=False)


def decode_calls(raw: str) -> list[Call]:
    try:
        return [Call(**item) for item in json.loads(raw)]
    except (json.JSONDecodeError, TypeError, ValueError):
        return []


# -- rendering --------------------------------------------------------------


def _plural(n: int, noun: str) -> str:
    return f"{n} {noun}" if n == 1 else f"{n} {noun}s"


def _seconds(value: float) -> str:
    return f"{value:.1f}s"


def _summary(calls: list[Call], *, interrupted: bool) -> str:
    """The suffixes a reader wants before deciding to click."""
    failed = sum(1 for call in calls if call.state == CALL_FAILED)
    parts = [_plural(len(calls), "tool")]
    if failed:
        parts.append(f"{failed} failed")
    if interrupted:
        parts.append("interrupted")
    return " · ".join(parts)


def render_counter(count: int) -> View:
    return View.of(f"⚙️ Running {_plural(count, 'tool')}…")


def render_collapsed(state: ScreenState, calls: list[Call], *, interrupted: bool) -> View:
    button = screen_action(
        state, id="open", label=f"Running {_summary(calls, interrupted=interrupted)} →",
        value="open",
    )
    return View(cards=(Card(text="", actions=(button,)),))


def render_expired(state: ScreenState) -> View:
    return View.of(_EXPIRED)


def _pretty(call: Call) -> str:
    """Arguments as a person reads them: one key per line."""
    try:
        return json.dumps(json.loads(call.args), indent=2, ensure_ascii=False)
    except (json.JSONDecodeError, TypeError, ValueError):
        return call.args  # a stored copy cut mid-JSON stays readable as text


def _cut(text: str, limit: int, *, total: int) -> str:
    if len(text) <= limit:
        return text
    return f"{text[:limit].rstrip()}\n… cut at {limit} of {total} chars"


def _row(call: Call, *, open_: bool) -> str:
    marker = "▾" if open_ else "▸"
    took = "—" if call.state == CALL_UNFINISHED else _seconds(call.duration_s)
    return f"{marker} {call.index + 1} · {call.tool} · {call.state} · {took}"


def render_accordion(
    state: ScreenState, calls: list[Call], *, page: int, expanded: int | None,
    interrupted: bool,
) -> View:
    """One card: a header, a row per call on this page, the open row's arguments
    under it, and the controls in one row underneath."""
    pages = max(1, (len(calls) + PAGE_SIZE - 1) // PAGE_SIZE)
    page = min(max(page, 0), pages - 1)
    shown = calls[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]
    total_s = sum(call.duration_s for call in calls)

    header = f"⚙️ {_plural(len(calls), 'tool call')} · {_seconds(total_s)}"
    failed = sum(1 for call in calls if call.state == CALL_FAILED)
    if failed:
        header += f" · {failed} failed"
    if interrupted:
        header += " · interrupted"
    if pages > 1:
        header += f" · page {page + 1}/{pages}"

    lines = [header]
    open_call = next((call for call in shown if call.index == expanded), None)
    for call in shown:
        lines.append(_row(call, open_=call is open_call))
        if call is open_call:
            lines.append("@ARGS@")
    body = "\n".join(lines)

    if open_call is not None:
        pretty = _pretty(open_call)
        total = open_call.cut or len(pretty)
        # What is left of the card once the rows are in it, and the display
        # cap — whichever is smaller is what the arguments get.
        room = CARD_MAX - len(body.replace("@ARGS@", "")) - 40
        block = preformatted(_cut(pretty, min(ARGS_SHOWN_MAX, room), total=total), lang="json")
        body = body.replace("@ARGS@", block)

    base = _state(state, page=page, expanded=expanded)
    controls: list[Action] = [
        screen_action(
            base, id=f"r{call.index}", label=str(call.index + 1), value=f"row:{call.index}",
            style="primary" if call is open_call else "",
        )
        for call in shown
    ]
    if pages > 1:
        if page > 0:
            controls.append(screen_action(base, id="prev", label="‹", value="prev"))
        if page < pages - 1:
            controls.append(screen_action(base, id="next", label="›", value="next"))
    controls.append(screen_action(base, id="close", label="✕ Close", value="close"))
    return View(cards=(Card(text=body, actions=tuple(controls)),))


def _state(state: ScreenState, *, page: int = 0, expanded: int | None = None) -> ScreenState:
    """The state a control carries: the token, and only the position that is
    not the default, so the encoded form stays as short as it can."""
    data: dict[str, Any] = {"t": state.data.get("t", "")}
    if page:
        data["p"] = page
    if expanded is not None:
        data["x"] = expanded
    return ScreenState(screen=state.screen, agent=state.agent, data=data)


# -- the screen: answering clicks -------------------------------------------


class TraceScreen:
    """Redraws the widget from the stored trace and the click's position.

    Registered like any screen so clicks route to it, but not listed: there is
    nothing to open by hand, and a model offered it as a panel would open an
    empty one.
    """

    command = TRACE_SCREEN
    listed = False

    def __init__(self, store: TraceStore) -> None:
        self._store = store

    async def admits(self, *, user_id: str, ref: ConversationRef | None) -> str:
        return "" if ref is None else _BY_HAND

    async def render(self, state: ScreenState, *, user_id: str) -> View:
        record = await self._store.get_trace(str(state.data.get("t") or ""))
        if record is None:
            return render_expired(state)
        calls = decode_calls(record.calls)
        interrupted = any(call.state == CALL_UNFINISHED for call in calls)
        value = str(state.data.get("value") or "")
        page = int(state.data.get("p") or 0)
        expanded = state.data.get("x")
        expanded = int(expanded) if expanded is not None else None

        if value == "close":
            return render_collapsed(_state(state), calls, interrupted=interrupted)
        if value == "open":
            # The failed call is what the panel is usually opened for.
            first_failed = next((c.index for c in calls if c.state == CALL_FAILED), None)
            expanded = first_failed
            page = 0 if expanded is None else expanded // PAGE_SIZE
        elif value in ("prev", "next"):
            page += 1 if value == "next" else -1
            expanded = None
        elif value.startswith("row:"):
            try:
                index = int(value.split(":", 1)[1])
            except ValueError:
                index = -1
            if 0 <= index < len(calls):
                expanded = None if expanded == index else index
                page = index // PAGE_SIZE
        return render_accordion(
            state, calls, page=page, expanded=expanded, interrupted=interrupted
        )


# -- recording one turn ------------------------------------------------------


class TurnTrace:
    """One turn's tool activity, from the first call to the button.

    ``on_event`` is synchronous and cheap on purpose: it runs inside the
    runtime's reader loop, where a wait would hold back every event behind
    it, so it only notes what happened and asks for a redraw. The redraws run
    on their own task — the first one right away, so the counter appears with
    the first call, then no more than one a second, whatever the burst.
    """

    def __init__(
        self, store: TraceStore, chat: ChatClient, record: SessionRecord, *,
        callback_url: str,
    ) -> None:
        self._store = store
        self._chat = chat
        self._record = record
        self._ref = conversation_ref(record)
        self._callback_url = callback_url
        self._token = secrets.token_hex(16)
        self._created_at = clock.now_iso()
        self._calls: list[Call] = []
        self._open: dict[str, int] = {}  # call id -> index of the call still running
        self._post_id: str | None = None
        self._closed = False
        self._dead = False  # the first post failed; nothing further is attempted
        self._dirty = False
        self._wake = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    @property
    def calls(self) -> list[Call]:
        return list(self._calls)

    def on_event(self, event: object) -> None:
        if self._closed:
            return
        if isinstance(event, ToolStarted):
            args = json.dumps(event.args, ensure_ascii=False, separators=(",", ":"))
            cut = len(args) if len(args) > ARGS_STORED_MAX else 0
            call = Call(
                index=len(self._calls), call_id=event.call_id, tool=event.tool,
                args=args[:ARGS_STORED_MAX], cut=cut,
            )
            self._calls.append(call)
            self._open[event.call_id] = call.index
        elif isinstance(event, ToolFinished):
            index = self._open.pop(event.call_id, None)
            if index is None:
                return
            call = self._calls[index]
            call.state = CALL_FAILED if event.is_error else CALL_OK
            call.duration_s = event.duration_s
        else:
            return
        # Only mark; never wake. The loop redraws once per interval while there
        # is something new, so a burst costs one redraw — an event that woke it
        # would cost one per event.
        self._dirty = True
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._redraws(), name=f"tooltrace:{self._token}")

    async def _redraws(self) -> None:
        while self._dirty and not self._closed and not self._dead:
            self._dirty = False
            self._wake.clear()
            await self._draw(render_counter(len(self._calls)))
            if self._closed:
                break  # end() arrived while drawing; it is waiting on us
            try:
                await asyncio.wait_for(self._wake.wait(), REDRAW_INTERVAL_S)
            except TimeoutError:
                pass

    async def _draw(self, view: View) -> None:
        cards = list(view.cards)
        try:
            if self._post_id is None:
                self._post_id = await self._chat.post_cards(
                    self._ref, cards, callback_url=self._callback_url
                )
            else:
                await self._chat.update_cards(
                    self._post_id, cards, callback_url=self._callback_url
                )
        except Exception:
            # A widget that cannot be drawn is not the turn's problem: log it,
            # stop trying, and leave the reply alone.
            logger.warning("tool trace could not be drawn for %s", self._record.agent, exc_info=True)
            self._dead = True

    async def end(self, *, interrupted: bool = False) -> None:
        """The turn is over: settle the counter into the button.

        The record is written before the button appears, so a click can never
        find nothing behind it. A turn that called no tool leaves no trace and
        no message.
        """
        if self._closed:
            return
        self._closed = True
        self._wake.set()
        if self._task is not None:
            try:
                await self._task
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("tool trace redraw failed", exc_info=True)
        if not self._calls or self._dead:
            return
        try:
            await self._store.create_trace(TraceRecord(
                token=self._token, agent=self._record.agent,
                channel_id=self._record.channel_id,
                conversation_id=self._record.conversation_id, kind=self._record.kind,
                created_at=self._created_at, finished_at=clock.now_iso(),
                calls=encode_calls(self._calls), post_id=self._post_id or "",
            ))
            state = ScreenState(screen=TRACE_SCREEN, agent=self._record.agent,
                                data={"t": self._token})
            await self._draw(render_collapsed(state, self._calls, interrupted=interrupted))
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("tool trace could not be finished", exc_info=True)


class ToolTrace:
    """Builds a trace for each turn, and sweeps old ones at startup."""

    def __init__(
        self, store: TraceStore, *, callback_url: str, retention_days: int = 14,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._store = store
        self._callback_url = callback_url
        self._retention_days = retention_days
        self._now = now or (lambda: datetime.now(timezone.utc))

    def begin(self, record: SessionRecord, chat: ChatClient) -> TurnTrace:
        return TurnTrace(self._store, chat, record, callback_url=self._callback_url)

    async def sweep(self) -> int:
        """Drop traces past the retention; 0 keeps every one."""
        if self._retention_days <= 0:
            return 0
        cutoff = self._now() - timedelta(days=self._retention_days)
        return await self._store.prune_traces(before=cutoff.isoformat(timespec="seconds"))
