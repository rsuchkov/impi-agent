"""TracerByGateway: one flow, a tracer per gateway the turn came through."""

from crucible.flows.tracers import NoTrace, TracerByGateway
from crucible.ports.agent.events import ToolStarted
from crucible.store.base import SessionRecord

RECORD = SessionRecord("assistant", "dm1", "dm1", "dm", "rs", "t", "t", last_gateway="slack")


class _Trace:
    def __init__(self, name: str) -> None:
        self.name = name

    def on_event(self, event) -> None:
        return None

    async def end(self, *, interrupted: bool = False) -> None:
        return None


class _Tracer:
    def __init__(self, name: str) -> None:
        self.name = name
        self.begun: list[tuple] = []

    def begin(self, record, chat, *, anchor=None, gateway=""):
        self.begun.append((record.conversation_id, anchor, gateway))
        return _Trace(self.name)


async def test_the_tracer_is_picked_by_the_gateway_the_turn_came_through() -> None:
    journal, widget = _Tracer("journal"), _Tracer("widget")
    tracers = TracerByGateway({"http": journal}, default=widget)
    chat = object()
    assert tracers.begin(RECORD, chat, gateway="http").name == "journal"  # type: ignore[attr-defined]
    assert tracers.begin(RECORD, chat, gateway="slack").name == "widget"  # type: ignore[attr-defined]
    assert tracers.begin(RECORD, chat, gateway="mattermost").name == "widget"  # type: ignore[attr-defined]
    # Each tracer is told what the flow knows: the anchor and the gateway.
    assert journal.begun == [("dm1", None, "http")] and widget.begun[0][2] == "slack"


async def test_a_gateway_nobody_traces_gets_a_trace_that_draws_nothing() -> None:
    tracers = TracerByGateway({"http": _Tracer("journal")})
    trace = tracers.begin(RECORD, object(), gateway="slack")  # type: ignore[arg-type]
    assert isinstance(trace, NoTrace)
    trace.on_event(ToolStarted("c1", "read", {}))  # harmless
    await trace.end()
