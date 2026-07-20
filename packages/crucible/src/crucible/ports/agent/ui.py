"""UiBridge port: surface a runtime's mid-turn interactive request to a human.

When an agent blocks mid-turn on an interactive request (confirm/select/input),
the concrete runtime calls this port to ask the user and get the answer back —
turning a blocking UI dialog into a gateway widget round-trip.

Kept in the agent-ports layer so the runtime depends only on it (never on
chat/store/gateway concretes); the composition root wires the implementation.
"""

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class UiRequest:
    """One interactive request the runtime is blocking on. Fields by method:
    ``confirm`` uses title+message, ``select`` uses title+options, ``input`` uses
    title+placeholder."""

    request_id: str
    method: str  # "confirm" | "select" | "input" | "editor"
    title: str = ""
    message: str = ""
    options: tuple[str, ...] = ()
    placeholder: str = ""


@dataclass(frozen=True)
class UiOutcome:
    """The human's answer. ``confirmed`` for confirm, ``value`` for select/input/
    editor, ``cancelled`` when dismissed or defaulted (timeout / no resolution)."""

    value: str | None = None
    confirmed: bool | None = None
    cancelled: bool = False


class UiBridge(Protocol):
    async def request(self, runtime_session_id: str, req: UiRequest) -> UiOutcome:
        """Show ``req`` to the user of the conversation ``runtime_session_id``
        identifies and await their answer. MUST always resolve — on timeout or an
        unresolvable conversation, return a safe default (cancelled / declined) so
        the agent's turn never hangs forever."""
        ...
