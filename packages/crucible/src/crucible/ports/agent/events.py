"""Runtime events a flow can read without knowing the runtime.

A runtime streams what it is doing through ``on_event``. Most of that stream is
the runtime's own vocabulary and crosses the port as-is, promising nothing
beyond a ``type``. The two events here are different: what tool ran, with what,
and how it ended is something a flow acts on — it is what a person watching the
turn is shown — so it crosses in a shape every runtime has to fill the same way.

Arguments travel; results deliberately do not. A result is the one place a
value the agent was granted could surface, and nothing built on these events is
allowed to see it.
"""

from dataclasses import dataclass, field
from typing import Any

TOOL_STARTED = "tool_started"
TOOL_FINISHED = "tool_finished"


@dataclass(frozen=True)
class ToolStarted:
    """A tool call has begun. ``call_id`` pairs it with its ``ToolFinished``."""

    call_id: str
    tool: str
    args: dict[str, Any] = field(default_factory=dict)
    type: str = field(default=TOOL_STARTED, init=False)


@dataclass(frozen=True)
class ToolFinished:
    """The call ended. ``duration_s`` is measured by the runtime driver between
    the two events, since not every runtime reports its own timing."""

    call_id: str
    tool: str
    is_error: bool = False
    duration_s: float = 0.0
    type: str = field(default=TOOL_FINISHED, init=False)
