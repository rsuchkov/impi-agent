"""Telemetry: what the engine did, told as events, for whoever wants to count.

The engine does not know what a dashboard looks like, which exporter the
deployment runs, or whether anything is kept at all: it reports what happened,
as data — a turn ended, a tool was called, a card was answered — and the
application decides where that goes (a metrics exporter, a log line, a table
of its own). Metrics are what the receiver derives; the engine sends the
facts. Every event is a frozen record of flat fields: names, numbers,
durations; never an object of the engine's, never the text people wrote.
``user_id`` is the platform's id as the gateway gave it — a receiver that must
not keep it hashes or maps it (``PersonKey``) on the way out.
"""

from dataclasses import dataclass
from typing import Protocol

from crucible.ports.agent.runtime import TokenUsage


@dataclass(frozen=True)
class TurnEnded:
    """One turn of an agent, as it ended."""

    agent: str
    gateway: str  # "" for a turn the engine started with no gateway named
    user_id: str  # whose message the turn answered ("" when nobody's)
    conversation_id: str  # the conversation's key, as the application keys it
    kind: str  # where the reply went: dm, thread, channel
    direct: bool  # only the person and the agent read it
    synthetic: bool  # a click, a command, a form, an engine-started turn
    messages: int  # how many messages the turn answered at once
    attachments: int  # how many files came with them
    outcome: str  # TurnOutcome, lower-cased: replied, acted, empty, truncated, timeout, error
    notice: str  # the notice code the person got, "" when none
    duration_s: float  # from the batch's arrival to the outcome
    model: str  # provider/model that answered, "" when the runtime did not say
    stop_reason: str  # the runtime's word for how the model stopped, "" when none
    tool_calls: tuple[str, ...]  # the tools the model called, in order
    truncated: bool
    usage: TokenUsage | None  # tokens and the runtime's own cost estimate, when it reported them


TelemetryEvent = TurnEnded


class Telemetry(Protocol):
    """Where the events go. ``record`` must not raise and must not block: the
    engine calls it on the turn's own path."""

    def record(self, event: TelemetryEvent) -> None: ...
