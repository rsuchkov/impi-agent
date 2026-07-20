"""PendingUiRequests: in-memory registry of outstanding blocking UI requests.

A blocking UI dialog (confirm/select) pauses the agent's turn while the human
answers. The bridge registers a one-shot ``token -> Future`` here and awaits it;
the integrations receiver resolves it on click. It's deliberately in-memory (not
SQLite): a paused turn can't survive a restart, so there's nothing to persist.

Shared by the bridge (register/await/cancel) and the receiver (resolve).
"""

import asyncio
import logging
from dataclasses import dataclass

from crucible.ports.agent.ui import UiOutcome

logger = logging.getLogger(__name__)

# The two confirm buttons' values, echoed back on click. Shared with the bridge
# that builds them so the mapping value -> confirmed stays in one place.
CONFIRM_YES = "Allow"
CONFIRM_NO = "Block"


@dataclass
class _Pending:
    future: "asyncio.Future[UiOutcome]"
    method: str
    agent: str
    conversation_id: str


class PendingUiRequests:
    def __init__(self) -> None:
        self._by_token: dict[str, _Pending] = {}

    def register(
        self, token: str, *, method: str, agent: str, conversation_id: str
    ) -> "asyncio.Future[UiOutcome]":
        future: asyncio.Future[UiOutcome] = asyncio.get_running_loop().create_future()
        self._by_token[token] = _Pending(future, method, agent, conversation_id)
        return future

    def discard(self, token: str) -> None:
        """Drop a token without resolving (poster failed / bridge timed out)."""
        self._by_token.pop(token, None)

    def resolve(self, token: str, value: str) -> bool:
        """Resolve a blocking request from a click. Returns False when ``token``
        is not a live blocking request, so the receiver falls through to the
        fire-and-forget path."""
        pending = self._by_token.pop(token, None)
        if pending is None or pending.future.done():
            return False
        if pending.method == "confirm":
            outcome = UiOutcome(confirmed=(value == CONFIRM_YES))
        else:  # select / input / editor: the value IS the answer
            outcome = UiOutcome(value=value)
        pending.future.set_result(outcome)
        return True

    def cancel_for_conversation(self, agent: str, conversation_id: str) -> int:
        """Cancel every outstanding request for a conversation — the user answered
        by typing instead of clicking, so the blocked turn should unblock with a
        cancelled outcome. Returns how many were cancelled."""
        hit = [
            token
            for token, p in self._by_token.items()
            if p.agent == agent and p.conversation_id == conversation_id
        ]
        for token in hit:
            pending = self._by_token.pop(token)
            if not pending.future.done():
                pending.future.set_result(UiOutcome(cancelled=True))
        if hit:
            logger.info("cancelled %d pending UI request(s) for %s", len(hit), conversation_id)
        return len(hit)
