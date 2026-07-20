"""WidgetPoster port: post an interactive message (buttons) as the agent.

Separate from ChatClient/ChatAdmin — a flow posts replies, a tool administers
channels, a widget poster posts clickable affordances whose clicks call back to
the engine. A gateway adapter implements it with the platform's interactive message actions.
"""

from typing import Protocol

from crucible.ports.chat.types import Action, ConversationRef, Form


class WidgetPoster(Protocol):
    async def post_actions(
        self, ref: ConversationRef, text: str, actions: list[Action], *, callback_url: str
    ) -> str:
        """Post ``text`` with clickable ``actions`` as the agent. Each action,
        when clicked, makes the platform POST to ``callback_url`` with the
        action's ``context``. Returns the posted message id."""
        ...

    async def retract(self, post_id: str, text: str) -> None:
        """Replace a previously-posted widget's text and drop its buttons — used
        when a blocking request expires or is cancelled, so no stale button can be
        clicked afterwards. Best-effort (a failure must not break the turn)."""
        ...

    async def open_dialog(
        self, trigger_id: str, form: Form, *, submit_url: str, state: str
    ) -> None:
        """Open ``form`` as a modal for the user whose click produced
        ``trigger_id`` (short-lived — call synchronously in the click handler). On
        submit the platform POSTs to ``submit_url`` with ``state`` echoed back."""
        ...


class WidgetService(Protocol):
    """What a tool calls to ask the user with buttons. Keeps the tool layer free
    of the store/poster concretes — the composition root wires those in.

    ``runtime_session_id`` ties the widget back to the conversation the calling turn
    runs inside (the tool forwards it opaquely); the service resolves where to
    post and registers the pending interaction."""

    async def ask(
        self,
        agent: str,
        runtime_session_id: str,
        prompt: str,
        options: list[str],
        *,
        style: str = "buttons",
    ) -> bool:
        """Post the choices (``style`` = "buttons" | "select"); return False if
        the conversation couldn't be resolved."""
        ...
