"""TurnRegistry: which turn is running on which session, for the length of
the turn.

The flow binds a message's ``TurnScope`` to the conversation's runtime session
before it runs the turn and unbinds it after; the tool server asks for the
scope by the session id a tool call carries. In memory on purpose — a scope
holds what must not be stored — and a leaf like ``loopguard``: it depends on
the turn port and nothing else, so both the flow and the tool layer may hold
one without either learning about the other.
"""

import contextlib
from collections.abc import AsyncGenerator

from crucible.ports.turn import TurnScope


class TurnRegistry:
    def __init__(self) -> None:
        self._by_session: dict[str, TurnScope] = {}

    def current(self, runtime_session_id: str) -> TurnScope | None:
        return self._by_session.get(runtime_session_id)

    @contextlib.asynccontextmanager
    async def bind(
        self, runtime_session_id: str, scope: TurnScope
    ) -> AsyncGenerator[None]:
        """Make ``scope`` the session's current turn until the block ends. A
        scope bound before (a turn still finishing while the next starts, in a
        flow that allows it) is restored afterwards rather than lost."""
        previous = self._by_session.get(runtime_session_id)
        self._by_session[runtime_session_id] = scope
        try:
            yield
        finally:
            if previous is None:
                self._by_session.pop(runtime_session_id, None)
            else:
                self._by_session[runtime_session_id] = previous
