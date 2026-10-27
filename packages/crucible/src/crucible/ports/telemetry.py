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


@dataclass(frozen=True)
class ToolCalled:
    """One call of a tool through the tool server, however it ended."""

    agent: str
    tool: str
    conversation_id: str  # the conversation's key; "" when the call named no known session
    user_id: str  # whose turn it was ("" when unknown)
    gateway: str  # the gateway the conversation is on ("" when unknown)
    outcome: str  # ok, error, crashed, unauthorized, unknown_tool, not_allowed, no_session,
    # not_in_conversation, not_through_gateway, unconfirmable, not_asked, denied, unanswered,
    # no_approver, abandoned
    duration_s: float
    confirmed: bool  # the tool is one a person confirms


@dataclass(frozen=True)
class CardAnswered:
    """A confirmation asked for a tool call, and how it ended."""

    agent: str
    tool: str
    outcome: str  # reused_grant, allowed_once, allowed_for, denied, unanswered, no_approver, abandoned
    wait_s: float  # how long the call waited on people
    grant_s: int  # the window opened, when one was


@dataclass(frozen=True)
class McpCalled:
    """One call of an MCP server's tool by the engine."""

    server: str
    tool: str  # the server's own name for it
    agent: str
    outcome: str  # ok, is_error, unreachable, unanswered, refused, token_refused, protocol, unsupported,
    # no_file, file_gone, file_changed, file_type, not_connected, not_direct, no_provider, provider_unavailable
    duration_s: float
    file_bytes: int  # a person's file sent with the call, in bytes (0 for none)


@dataclass(frozen=True)
class McpChecked:
    """One server's tools compared with their snapshot."""

    server: str
    result: str  # connected, unreachable, withdrawn, refreshed, restored, unpinned


@dataclass(frozen=True)
class TaskRan:
    """One occurrence of a scheduled task, as it finished."""

    agent: str
    task: str
    outcome: str  # the run's status word (store.base.RUN_STATUSES): ok, empty, timeout, error, deadline, no_agent, …
    duration_s: float


@dataclass(frozen=True)
class ConnectionChanged:
    """A person's connected account moved: as the connections audit says it."""

    provider: str
    event: str  # connect_start, connect, refresh, forget, disconnect, grant
    result: str  # ok, unavailable, rejected, not_connected, revoke_failed; on a connect also the link's
    # word: expired, foreign, denied, account_refused


@dataclass(frozen=True)
class EngineState:
    """How the engine stands, told on a timer: what a gauge reads, where the
    other events are what a counter sums."""

    agents: int  # agents running in this process
    sessions_alive: int  # runtime sessions with a process
    sessions_busy: int  # of them, with a turn in flight
    capacity: int  # how many the runtime admits at once
    turns_waiting: int  # turns waiting for a slot
    http_turns_active: int  # turns in flight on the http gateway (0 without one)


TelemetryEvent = (
    TurnEnded | ToolCalled | CardAnswered | McpCalled | McpChecked | TaskRan | ConnectionChanged | EngineState
)


class Telemetry(Protocol):
    """Where the events go. ``record`` must not raise and must not block: the
    engine calls it on the turn's own path."""

    def record(self, event: TelemetryEvent) -> None: ...
