"""TurnRegistry: which turn is running on which session, for the length of
the turn.

The flow binds a message's ``TurnScope`` and where the turn answers (its
``TurnPlace``) to the conversation's runtime session before it runs the turn
and unbinds them after; the tool server asks for the scope by the session id a
tool call carries, and whatever posts for the turn — the tool gate, a widget,
a file — asks for the place. In memory on purpose — a scope holds what must
not be stored, and where a turn answers is nobody's business once it has —
and a leaf like ``loopguard``: it depends on the turn port and nothing else,
so the flow, the tool layer and the interactions may all hold one without any
of them learning about the others.
"""

import contextlib
from collections.abc import AsyncGenerator

from crucible.ports.turn import TurnEvents, TurnPlace, TurnScope


class TurnRegistry:
    def __init__(self) -> None:
        self._by_session: dict[str, tuple[TurnScope | None, TurnPlace | None, TurnEvents | None]] = {}

    def current(self, runtime_session_id: str) -> TurnScope | None:
        bound = self._by_session.get(runtime_session_id)
        return bound[0] if bound else None

    def place(self, runtime_session_id: str) -> TurnPlace | None:
        bound = self._by_session.get(runtime_session_id)
        return bound[1] if bound else None

    def feed(self, runtime_session_id: str, event: object) -> None:
        bound = self._by_session.get(runtime_session_id)
        if bound is not None and bound[2] is not None:
            bound[2](event)

    @contextlib.asynccontextmanager
    async def bind(
        self, runtime_session_id: str, scope: TurnScope | None, *,
        place: TurnPlace | None = None, events: TurnEvents | None = None,
    ) -> AsyncGenerator[None]:
        """Make ``scope``, ``place`` and ``events`` the session's current turn
        until the block ends. A turn bound before (one still finishing while
        the next starts, in a flow that allows it) is restored afterwards
        rather than lost."""
        previous = self._by_session.get(runtime_session_id)
        self._by_session[runtime_session_id] = (scope, place, events)
        try:
            yield
        finally:
            if previous is None:
                self._by_session.pop(runtime_session_id, None)
            else:
                self._by_session[runtime_session_id] = previous
