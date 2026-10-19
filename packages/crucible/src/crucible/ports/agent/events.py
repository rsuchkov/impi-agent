"""Runtime events a flow can read without knowing the runtime.

A runtime streams what it is doing through ``on_event``. Most of that stream is
the runtime's own vocabulary and crosses the port as-is, promising nothing
beyond a ``type``. The events here are different: what tool ran, with what, and
how it ended, and what the model said on its way to the next call, are things
a flow acts on — they are what a person watching the turn is shown — so they
cross in a shape every runtime has to fill the same way.

Arguments travel; results deliberately do not. A result is the one place a
value the agent was granted could surface, and nothing built on these events is
allowed to see it. The model's reasoning does not travel either: a progress
remark is text the model wrote for the person, a thought is not.
"""

from dataclasses import dataclass, field
from typing import Any

TOOL_STARTED = "tool_started"
TOOL_FINISHED = "tool_finished"
TEXT_PROGRESS = "text_progress"


@dataclass(frozen=True)
class ToolStarted:
    """A tool call has begun. ``call_id`` pairs it with its ``ToolFinished``.

    ``caption`` is a short line for a person watching — "Reading PROJ-123" —
    written by whoever knows the tool (see ``ports.agent.captions``), never by
    the model and never the arguments themselves: an argument is a whole script
    or a whole comment as often as it is a key. A runtime leaves it empty."""

    call_id: str
    tool: str
    args: dict[str, Any] = field(default_factory=dict)
    caption: str = ""
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


@dataclass(frozen=True)
class TextProgress:
    """Text the model wrote and then went on to call tools: a remark on the way
    ("Found the workflow, now listing transitions"), not the answer. The answer
    is what the model says after its last call, and travels in the result."""

    text: str
    type: str = field(default=TEXT_PROGRESS, init=False)
