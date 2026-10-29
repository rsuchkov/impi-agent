"""What belongs to one turn and nothing else — handed to the tools that run
inside it, and gone when it ends.

A tool is reached through a session id, which names the conversation; two turns
of the same conversation look alike from inside a tool. Some things are the
turn's, though: a credential the person sent along with this message and that
must never be stored, a request id to log against, a note the tool leaves for
whoever started the turn ("the session has expired"). ``TurnScope`` carries
them, the flow binds it to the session for the length of the turn, and a tool
reads it from its context. Secrets live in ``TurnSecrets``, which shows its
names and never its values: a scope ends up in logs and tracebacks, and the one
thing a per-turn credential must not do is outlive the turn in writing.

Where the turn answers is the turn's too: ``TurnPlace`` is the gateway the
message came through and the thread the answer goes in, bound beside the
scope, so that whatever the turn posts while it runs — a confirmation card, a
question with buttons, a file — goes where the answer goes (a direct message
answered in a thread under the person's message, say) and not merely to the
conversation's standing address.
"""

from collections.abc import Iterator, Mapping
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from typing import NoReturn, Protocol

# The attribute a dispatcher puts on the scope of a turn the engine started
# with no scope of the application's: nobody is behind this turn, so a tool
# that would act as a person (their connected accounts, say) must not. An
# application that hands the turn a scope of its own has decided that itself.
ENGINE_TURN_ATTRIBUTE = "engine_turn"


class TurnSecrets:
    """Named values a tool may use for this turn only. Says which names it
    holds, never what they are: ``repr``, ``str`` and pickling give nothing
    away, so a scope caught in a log line or a crash report is harmless."""

    __slots__ = ("_values",)

    def __init__(self, values: Mapping[str, str] | None = None) -> None:
        self._values = dict(values or {})

    def get(self, name: str) -> str | None:
        return self._values.get(name)

    def names(self) -> frozenset[str]:
        return frozenset(self._values)

    def __contains__(self, name: object) -> bool:
        return name in self._values

    def __iter__(self) -> Iterator[str]:
        return iter(self._values)

    def __len__(self) -> int:
        return len(self._values)

    def __repr__(self) -> str:
        return f"TurnSecrets({sorted(self._values)})"

    __str__ = __repr__

    def __reduce__(self) -> NoReturn:
        raise TypeError("a turn's secrets cannot be pickled")


@dataclass(frozen=True)
class TurnScope:
    """One turn's own data. ``attributes`` are plain facts a tool may read (a
    request id, a locale); ``secrets`` are values it may use and must not
    repeat; ``flags`` are notes a tool leaves for the flow that started the
    turn — codes, not prose, for the flow to act on once the turn is over."""

    turn_id: str
    secrets: TurnSecrets = field(default_factory=TurnSecrets, repr=False)
    attributes: Mapping[str, str] = field(default_factory=dict)
    flags: set[str] = field(default_factory=set, compare=False)

    def flag(self, code: str) -> None:
        """Leave a note for whoever started the turn."""
        self.flags.add(code)


@dataclass(frozen=True)
class TurnPlace:
    """Where a turn answers: the gateway its message came through and the
    thread the answer goes in — "" when it goes at the top of the channel or
    the direct message. Where the turn's own posts go while it runs."""

    gateway: str
    thread_root_id: str = ""


class TurnScopes(Protocol):
    """Where a tool's context is filled from: the scope bound to a session
    right now, or None between turns."""

    def current(self, runtime_session_id: str) -> TurnScope | None: ...


class TurnPlaces(Protocol):
    """Where the turn on a session answers right now, or None between turns."""

    def place(self, runtime_session_id: str) -> TurnPlace | None: ...


class TurnBinding(Protocol):
    """What a flow holds: bind a turn — its scope, if the message carried one,
    and where it answers — to a session for exactly that turn."""

    def bind(
        self, runtime_session_id: str, scope: TurnScope | None, *, place: TurnPlace | None = None
    ) -> AbstractAsyncContextManager[None]: ...
