"""People's own accounts at OAuth providers, as this engine wires them.

``crucible.connections`` is the machinery; what is decided here is impi's:

- whose connections a turn uses: the chat user it acts for, as
  ``<gateway>:<user id>`` — one person in two chats is two owners;
- which account may be bound to whom: with ``CONNECTION_MATCH_EMAIL__<NAME>``
  (on by default) the account's email must be the one the chat profile shows,
  and a profile that shows none cannot be matched;
- which agents may use which provider (``CONNECTION_AGENTS__<NAME>``);
- where a person hears that a connect worked: the message they started it
  from is drawn again (``RedrawOnConnect``).

Built only when some provider is configured; then the sealing keys and the
public callback address are required, and their absence stops the engine with
the key to set rather than leaving connects that cannot finish.
"""

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass

from crucible.connections import (
    Connected,
    ConnectionsError,
    Grant,
    OAuthConnections,
    ProviderAccount,
)
from crucible.connections.access import ConnectionAccess, ConnectionAccessFactory
from crucible.connections.callback import CallbackServer
from crucible.connections.oauth2 import OAuth2Provider
from crucible.gateways import GatewayConfig
from crucible.gateways.dispatch import GatewayDispatcher
from crucible.ports.chat.client import ChatClient
from crucible.ports.turn import TurnScope
from crucible.store.base import Store
from impi.config import ImpiSettings
from impi.connections_config import ConnectionProvider
from impi.connections_screen import CONNECTED, ConnectReturn


def owner_of(gateway: str, user_id: str) -> str | None:
    """Whose connections: the chat user on the gateway they were seen on."""
    return f"{gateway}:{user_id}" if gateway and user_id else None


def user_of(owner: str) -> str:
    """The chat user an owner is, on its gateway — ``owner_of`` backwards."""
    return owner.partition(":")[2]


class RedrawOnConnect:
    """Once a connect is stored, draw the message it was started from again —
    the offer a tool posted, or the panel — so the person sees it worked where
    they clicked. ``dispatcher`` is set once interactivity is built; until
    then, and for a connect started from nowhere, nothing is drawn."""

    def __init__(self) -> None:
        self.dispatcher: GatewayDispatcher | None = None

    async def __call__(self, connected: Connected) -> None:
        back = ConnectReturn.decode(connected.return_to)
        if back is None or self.dispatcher is None:
            return
        await self.dispatcher.redraw_screen(
            back.state, f"{CONNECTED}:{connected.provider}",
            post_id=back.post_id, user_id=user_of(connected.owner),
        )


class ChatEmailPolicy:
    """The account's email must be the chat user's, where the provider asks for
    it. The chat profile is read from a client of the owner's gateway."""

    def __init__(
        self, providers: Mapping[str, ConnectionProvider], client_for: Callable[[str], ChatClient | None],
    ) -> None:
        self._providers = providers
        self._client_for = client_for

    async def check(self, owner: str, provider: str, account: ProviderAccount) -> str | None:
        settings = self._providers.get(provider)
        if settings is None or not settings.match_email:
            return None
        client = self._client_for(owner.partition(":")[0])
        profile = await client.get_user_profile(user_of(owner)) if client is not None else None
        mine = (profile.email if profile is not None else "").strip().lower()
        if not mine:
            return "your chat account shows no email to compare it with"
        if (account.email or "").strip().lower() != mine:
            return "its email is not the one your chat account has"
        return None


class NarrowedConnections:
    """One person's connections, narrowed to the providers one agent may use."""

    def __init__(self, inner: ConnectionAccess, allowed: tuple[str, ...]) -> None:
        self._inner = inner
        self._allowed = allowed

    @property
    def providers(self) -> tuple[str, ...]:
        return self._allowed

    async def grant(self, provider: str) -> Grant:
        self._require(provider)
        return await self._inner.grant(provider)

    async def forget(self, grant: Grant) -> None:
        await self._inner.forget(grant)

    async def account(self, provider: str) -> ProviderAccount | None:
        self._require(provider)
        return await self._inner.account(provider)

    def _require(self, provider: str) -> None:
        if provider not in self._allowed:
            raise ConnectionsError(f"{provider} is not a provider this agent may use")


@dataclass
class Connections:
    """Connected accounts as this deployment built them: the service, the
    callback page's listener, the configured providers and whose a turn's
    connections are."""

    service: OAuthConnections
    callback: CallbackServer
    providers: dict[str, ConnectionProvider]
    owner_for: Callable[[str, str], str | None]

    def providers_for(self, agent: str) -> tuple[str, ...]:
        return tuple(name for name, p in sorted(self.providers.items()) if p.allows(agent))

    def tool_access(self) -> ConnectionAccessFactory:
        """What the tool server hands a tool: the person's connections, as far
        as the agent calling may use them."""

        def factory(agent: str, user_id: str, turn: TurnScope | None) -> ConnectionAccess | None:
            owner = self.owner_for(agent, user_id)
            allowed = self.providers_for(agent)
            if owner is None or not allowed:
                return None
            return NarrowedConnections(self.service.for_owner(owner), allowed)

        return factory


def build_connections(
    settings: ImpiSettings,
    sessions: Store,
    configs: Mapping[str, GatewayConfig | None],
    client_of: Callable[[str], ChatClient | None],
    *,
    on_connected: Callable[[Connected], Awaitable[None]] | None = None,
) -> Connections | None:
    """None when no provider is configured. ``client_of(agent)`` is that
    agent's chat client, read when an email is checked; ``on_connected`` hears
    of every connect stored."""
    providers = settings.connection_providers()
    if not providers:
        return None
    keys = settings.sealing_keys()
    if not keys:
        raise ValueError(
            f"CONNECTION_*__ configures {', '.join(sorted(providers))}, but CONNECTIONS_KEYS is empty — "
            "generate one with `impi connections key`"
        )
    if not settings.connections_public_url:
        raise ValueError(
            "CONNECTIONS_PUBLIC_URL is required with connections: the address a person's browser "
            "reaches the callback on, registered with each provider as <it>/oauth/callback"
        )
    redirect = settings.connections_redirect_uri
    try:
        # Deferred for the reason crucible.store defers pymongo: the sealing
        # module needs cryptography, an extra, and a deployment that connects
        # no accounts must not pay for it. Caught here, at composition, and
        # answered with the install command.
        from crucible.connections.sealing import FernetSealer  # noqa: PLC0415
    except ImportError as exc:  # pragma: no cover - depends on how it was installed
        raise RuntimeError(
            "connections need the 'connections' extra: install impi[connections] "
            "(or add cryptography>=43.0)"
        ) from exc

    def client_for(gateway: str) -> ChatClient | None:
        for agent, config in configs.items():
            if config is not None and config.kind == gateway and (client := client_of(agent)) is not None:
                return client
        return None

    service = OAuthConnections(
        connections=sessions, links=sessions,
        providers={name: OAuth2Provider(p.oauth2_config(redirect)) for name, p in providers.items()},
        sealer=FernetSealer(keys),
        policy=ChatEmailPolicy(providers, client_for),
        on_connected=on_connected,
    )

    def owner_for(agent: str, user_id: str) -> str | None:
        config = configs.get(agent)
        return owner_of(config.kind, user_id) if config is not None else None

    return Connections(
        service=service,
        callback=CallbackServer(service, host=settings.connections_host, port=settings.connections_port),
        providers=providers,
        owner_for=owner_for,
    )
