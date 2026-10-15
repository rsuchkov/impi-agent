"""What a consumer of the approval primitive exposes to the layer that calls it.

Declared here, with no dependencies of its own, so the tool layer can require an
approval without importing the store or the chat clients that producing one
needs.
"""

from dataclasses import dataclass
from enum import Enum, auto
from typing import Any, Protocol

from crucible.ports.chat.types import CallPreview


class ConfirmOutcome(Enum):
    """How asking ended. Only ALLOWED runs the call; the three refusals are
    kept apart because the model is told which one it was — a person's no and
    a person's absence call for different next steps."""

    ALLOWED = auto()
    DENIED = auto()  # a person said no
    UNANSWERED = auto()  # nobody answered before the question expired
    NO_APPROVER = auto()  # nowhere to ask, or the question could not be posted


@dataclass(frozen=True)
class NotAsked:
    """The gate did not put the question to anyone, for a reason of its own —
    a budget of questions spent for this turn, a policy that refuses the call
    outright — and says the reason, which the model hears as
    ``not asked: <reason>``. The one outcome with words of the gate's in it,
    because the three ``ConfirmOutcome`` refusals are facts about a person
    and this one is a fact about the gate."""

    reason: str


class ToolApproving(Protocol):
    """Ask a human whether a tool call may go ahead.

    The tool server holds one of these and consults it before executing a tool
    that declares ``requires_confirmation``. Anything but ``ALLOWED`` means the
    call does not happen, and says why: a refusal, a silence, nobody available
    to ask — or ``NotAsked``, when the gate never asked and says why. ``preview``
    is what the tool itself says the call would do (``Tool.describe``), for the
    card; None means the person is shown the arguments. ``display_name`` is the
    tool's name for a person (``Tool.display_name``), "" when it declares none.
    """

    async def confirm(
        self,
        agent: str,
        tool: str,
        args: dict[str, Any],
        *,
        runtime_session_id: str,
        preview: CallPreview | None = None,
        display_name: str = "",
    ) -> ConfirmOutcome | NotAsked: ...
