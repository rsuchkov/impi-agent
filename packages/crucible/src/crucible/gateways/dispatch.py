"""The narrow view of the interaction dispatcher a gateway forwards clicks and
submissions to.

A gateway depends on this protocol, not the store-backed InteractionDispatcher
concrete — that keeps the gateway layer free of the store (per the layer contract)
while still routing interactive callbacks to the neutral dispatch brain.
"""

from typing import Protocol

from crucible.ports.chat.types import Form


class FormOpen(Protocol):
    """What a form-open needs from the dispatcher's load_form result."""

    @property
    def form(self) -> Form: ...


class GatewayDispatcher(Protocol):
    def resolve_pending(self, token: str, value: str) -> bool: ...
    async def consume_action(self, token: str, value: str, user_id: str) -> object: ...
    async def load_form(self, form_token: str) -> FormOpen | None: ...
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
    ) -> object:
        """Run a command (slash command / message shortcut) as a private turn of
        ``agent`` in the conversation it was invoked from."""
        ...
