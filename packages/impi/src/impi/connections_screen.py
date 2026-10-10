"""The `/connections` screen: a person's own accounts at the OAuth providers an
agent may use — connect one, see which, end it.

Opened in a direct message with the agent and nowhere else (``direct_only``):
it shows whose accounts are connected, and that is nobody else's business. The consent link
a Connect gives is the clicker's alone too — whoever holds it can finish the
connect — so it is a private note beside the redraw, never part of the
message. Every other click redraws this one message.

State: nothing of its own; ``value`` carries what the last control returned
(``connect:<provider>``, ``disconnect:<provider>``).
"""

import logging
from collections.abc import Callable

from crucible.connections import ConnectionsError, OAuthConnections
from crucible.interactions.screens import ScreenState, View, screen_action
from crucible.ports.chat.types import Card

logger = logging.getLogger(__name__)

DEFAULT_COMMAND = "connections"

_CONNECT, _DISCONNECT = "connect", "disconnect"
_ACCENT_HEADER = "#7a5299"
_ACCENT_CONNECTED = "#3db887"
_ACCENT_IDLE = "#8e9297"



class ConnectionsScreen:
    # Whose accounts are connected is nobody else's business: a direct
    # message with the agent, and nowhere else.
    direct_only = True

    def __init__(
        self,
        connections: OAuthConnections,
        *,
        owner_for: Callable[[str, str], str | None],
        providers_for: Callable[[str], tuple[str, ...]],
        command: str = DEFAULT_COMMAND,
    ) -> None:
        self.command = command.lstrip("/").strip().lower() or DEFAULT_COMMAND
        self._connections = connections
        # (agent, user id) -> whose connections those are; (agent) -> which
        # providers that agent may use.
        self._owner_for = owner_for
        self._providers_for = providers_for

    async def render(self, state: ScreenState, *, user_id: str) -> View:
        owner = self._owner_for(state.agent, user_id)
        providers = self._providers_for(state.agent)
        if owner is None or not providers:
            return View.of("### 🔗 Connections\nThis agent uses no connected accounts.", accent=_ACCENT_IDLE)
        value = str(state.data.get("value") or "")
        verb, _, provider = value.partition(":")
        note, private_note = "", ""
        if provider in providers:
            try:
                if verb == _CONNECT:
                    url = await self._connections.begin(owner, provider)
                    private_note = (
                        f"Open this link to connect your {provider} account — it is yours alone, "
                        f"works once, and expires in ten minutes: {url}"
                    )
                elif verb == _DISCONNECT:
                    ended = await self._connections.disconnect(owner, provider)
                    note = (
                        f"Not connected to {provider}." if ended is None
                        else f"Disconnected from {provider}." if ended.revoked
                        else f"Disconnected from {provider} here, but {provider} did not confirm the "
                             "revoke — remove the application in your account settings there."
                    )
            except ConnectionsError:
                logger.warning("connections screen: %s %s failed", verb, provider, exc_info=True)
                note = f"{provider} did not answer; try again in a minute."
        view = await self._index(state.with_data(value=""), owner, providers, note=note)
        return View(cards=view.cards, private_note=private_note)

    async def _index(
        self, state: ScreenState, owner: str, providers: tuple[str, ...], *, note: str = "",
    ) -> View:
        connected = await self._connections.connected(owner)
        header = "### 🔗 Connections\nYour accounts this agent may act as, with your rights there."
        cards = [Card(text=header + (f"\n\n{note}" if note else ""), accent=_ACCENT_HEADER)]
        for provider in providers:
            account = connected.get(provider)
            if account is not None:
                who = account.username or account.name or account.id
                cards.append(Card(
                    text=f"**{provider}** — connected as **{who}**",
                    actions=(screen_action(state, id=f"d-{provider}", label="Disconnect",
                                           value=f"{_DISCONNECT}:{provider}", style="danger"),),
                    accent=_ACCENT_CONNECTED,
                ))
            else:
                cards.append(Card(
                    text=f"**{provider}** — not connected",
                    actions=(screen_action(state, id=f"c-{provider}", label="Connect",
                                           value=f"{_CONNECT}:{provider}", style="primary"),),
                    accent=_ACCENT_IDLE,
                ))
        return View(cards=tuple(cards))
