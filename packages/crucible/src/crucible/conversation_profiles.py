"""A conversation's profile: the agent's, narrowed and extended by what the
application says the conversation may do — the neutral half.

The flow hands ``AgentFlow(..., conversation_profiles=…)`` the agent's profile,
an ``Entitlement`` (``crucible.ports.entitlement``) and the runtime session id
before every turn. ``EntitledProfiles`` keeps the engine's side in step — the
tool manifest the conversation's process advertises from and the tool server's
allowlist for this session, both from ``ToolWiring.session_env`` and both from
the one entitlement, so what the process offers and what the server runs
cannot disagree — and asks the runtime driver's ``ProfileNarrowing`` for the
profile itself, since what a profile looks like is the driver's business and
each driver ships its own.

The runtime restarts the conversation's process when the profile it is handed
differs from the one the process was started with, and resumes the
conversation's memory from disk. The default entitlement gives the agent's own
profile back, with the per-session allowlist cleared, so a conversation whose
entitlement widens again gets everything its agent has.

The other half of the same mechanism is where an entitlement comes from.
``GatewayEntitlements`` reads one off the agent's own profile —
``runtime.tools_by_gateway``, what a conversation on each gateway may use —
and joins it with an application's own source when there is one, so the
engine's rule and the application's narrow the same profile. On the MCP
gateway it reads the token's scopes too: a client whose token may only read
gets the agent without the tools that would ask the person for a yes.
"""

from collections.abc import Callable, Collection

from crucible.ports.agent.runtime import AgentProfile
from crucible.ports.agent.spec import AgentSpec
from crucible.ports.chat.gateway import GATEWAY_MCP, MCP_SCOPES_ATTRIBUTE
from crucible.ports.chat.types import IncomingMessage
from crucible.ports.entitlement import (
    Entitlement,
    EntitlementSource,
    ProfileNarrowing,
    shared_entitlement,
)
from crucible.tools.registry import ToolRegistry
from crucible.tools.wiring import ToolWiring, declared


class EntitledProfiles:
    """``ConversationProfiles`` over the application's ``ToolWiring`` and its
    runtime driver's ``ProfileNarrowing``."""

    def __init__(self, tools: ToolWiring, narrow: ProfileNarrowing) -> None:
        self._tools = tools
        self._narrow = narrow

    def build(
        self, agent: str, profile: AgentProfile, entitlement: Entitlement, runtime_session_id: str
    ) -> AgentProfile:
        env = self._tools.session_env(agent, runtime_session_id, entitlement.tools)
        # Only a tool the manifest advertises can be held back — the runtime's
        # built-ins are not in it and are declared as they are; the driver is
        # told nothing of a name it would only tell the model a falsehood about.
        registry = self._tools.registry
        deferred = (
            tuple(n for n in entitlement.deferred if registry is not None and registry.get(n) is not None)
            if entitlement.deferred else None
        )
        return self._narrow(
            profile, tools=entitlement.tools, instructions=entitlement.instructions, env=env, deferred=deferred,
        )


class GatewayEntitlements:
    """An ``EntitlementSource`` from the agent's own profile: what
    ``runtime.tools_by_gateway`` says a conversation on the message's gateway
    may use (``AgentSpec.tools_on``), narrowed again by
    ``runtime.tools_in_channels`` where other people read the conversation — a
    channel, a thread — and nothing else: no instructions, no view of who the
    person is. Given an ``inner`` source (an application's own answer about
    the person or the place), the two are joined as several people's answers
    are: the tools both allow, the inner's instructions. Given the tool
    ``registry``, a turn an MCP client opened with a token that may not act
    (``MCP_SCOPES_ATTRIBUTE`` without ``act``) is narrowed to the tools that
    ask nobody for a yes — the read door."""

    def __init__(
        self, spec_of: Callable[[str], AgentSpec | None], *, inner: EntitlementSource | None = None,
        registry: ToolRegistry | None = None,
    ) -> None:
        self._spec_of = spec_of
        self._inner = inner
        self._registry = registry

    async def entitlement(
        self, agent: str, msg: IncomingMessage, *, tools: Collection[str] | None = None
    ) -> Entitlement:
        spec = self._spec_of(agent)
        own = Entitlement(tools=_tools_on(spec, msg.gateway) if spec is not None else None)
        if spec is not None and spec.tools_in_channels is not None and not msg.direct:
            own = shared_entitlement([own, Entitlement(tools=spec.tools_in_channels)])
        if spec is not None and self._registry is not None and _mcp_read_only(msg):
            ceiling = own.tools if own.tools is not None else spec.tools
            own = Entitlement(tools=_asking_nobody(self._registry, tuple(ceiling)))
        if tools is not None:
            own = shared_entitlement([own, Entitlement(tools=tools)])
        if self._inner is None:
            return own
        # The inner source is told the ceiling the gateway sets, so instructions
        # that speak of tools speak of the ones the conversation will have.
        theirs = await self._inner.entitlement(agent, msg, tools=own.tools)
        joined = shared_entitlement([own, theirs])
        return Entitlement(
            tools=joined.tools, instructions=theirs.instructions, deferred=theirs.deferred, agents=joined.agents,
        )


def _mcp_read_only(msg: IncomingMessage) -> bool:
    """Whether the turn is an MCP client's whose token may not act."""
    if msg.gateway != GATEWAY_MCP or msg.turn is None:
        return False
    return "act" not in msg.turn.attributes.get(MCP_SCOPES_ATTRIBUTE, "").split()


def _asking_nobody(registry: ToolRegistry, names: tuple[str, ...]) -> tuple[str, ...]:
    """``names`` — a server's tools spelled out — without the ones that would
    ask the person for a yes. A runtime's built-in is not in the registry and
    asks nobody; it stays."""
    kept: list[str] = []
    for name in declared(registry, names):
        tool = registry.get(name)
        if tool is None or not tool.requires_confirmation:
            kept.append(name)
    return tuple(kept)


def _tools_on(spec: AgentSpec, gateway: str) -> tuple[str, ...] | None:
    """The gateway's list; for a message that names no gateway on an agent
    that narrows some, the tools every named gateway allows — a door nobody
    named opens no wider than the narrowest one that is."""
    if gateway or not spec.tools_by_gateway:
        return spec.tools_on(gateway)
    allowed: set[str] | None = None
    for _, names in spec.tools_by_gateway:
        allowed = set(names) if allowed is None else allowed & set(names)
    first = spec.tools_by_gateway[0][1]
    return tuple(n for n in first if allowed is not None and n in allowed)
