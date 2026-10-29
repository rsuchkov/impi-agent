"""The narrow view of the interaction dispatcher a gateway forwards clicks and
submissions to.

A gateway depends on this protocol, not the store-backed InteractionDispatcher
concrete — that keeps the gateway layer free of the store (per the layer contract)
while still routing interactive callbacks to the neutral dispatch brain.
"""

from typing import Protocol

from crucible.approvals import ApprovalOutcome
from crucible.interactions.results import TurnStop
from crucible.interactions.screens import Redrawn, ScreenOpened
from crucible.ports.chat.types import Form, QuotedMessage


class FormOpen(Protocol):
    """What a form-open needs from the dispatcher's load_form result."""

    @property
    def form(self) -> Form: ...


class GatewayDispatcher(Protocol):
    async def resolve_approval(
        self, token: str, value: str, user_id: str, *, gateway: str = ""
    ) -> ApprovalOutcome:
        """Answer a request for authorization. Unlike every other click, this
        one may be addressed to particular people, so the result says whether
        the click was allowed to decide as well as whether it was recognized;
        ``gateway`` is the one the click came through, which is what tells who
        ``user_id`` is. A coroutine: the question may be waiting in another
        replica."""
        ...

    async def may_answer(self, token: str, user_id: str, *, gateway: str = "") -> bool:
        """Whether the request is waiting and addressed to this person (as the
        gateway knows them) — asked before a card is shown, judged as a click
        would be. A coroutine: the question may be waiting in another replica."""
        ...

    async def resolve_pending(self, token: str, value: str) -> bool: ...
    async def consume_action(
        self, token: str, value: str, user_id: str, *, pick: str = "", action_id: str = "", data: str = "",
    ) -> object:
        """A fire-and-forget click fed back as a turn; ``action_id`` and
        ``data`` are the control's own and the application's, for the
        message's ``click``."""
        ...
    async def load_form(self, form_token: str) -> FormOpen | None: ...
    async def open_screen(
        self, agent: str, command: str, *, channel_id: str, conversation_id: str,
        kind: str, user_id: str, gateway: str = "",
    ) -> ScreenOpened:
        """A command the engine may own — a screen, or the word that stops the
        turn — answered without an agent; ``owned`` False means route it on.
        ``gateway`` is the one it came through."""
        ...

    @property
    def stop_command(self) -> str:
        """The command word that stops a turn ("" = none), so a reaction mapped
        to it stops the turn instead of starting one."""
        ...

    async def stop_turn(self, agent: str, conversation_id: str, *, gateway: str = "") -> TurnStop:
        """Stop the agent's turn in the conversation as ``gateway`` names it."""
        ...
    async def redraw_screen(
        self, state_raw: str, value: str, *, post_id: str, user_id: str
    ) -> Redrawn | None: ...
    async def submit_form(
        self, state: str, submission: dict, cancelled: bool, user_id: str
    ) -> object: ...
    def invoke_command(
        self,
        agent: str,
        *,
        channel_id: str,
        conversation_id: str,
        kind: str,
        text: str,
        user_id: str,
        username: str = "",
        quoted: QuotedMessage | None = None,
        gateway: str = "",
    ) -> object:
        """Run a command (slash command, message shortcut, reaction) as a turn
        of ``agent`` in the conversation it was invoked from; ``quoted`` is the
        message it points at, for the flow to quote under its author's name."""
        ...
