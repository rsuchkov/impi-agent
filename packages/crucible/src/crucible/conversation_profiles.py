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
engine's rule and the application's narrow the same profile.
"""

from collections.abc import Callable

from crucible.ports.agent.runtime import AgentProfile
from crucible.ports.agent.spec import AgentSpec
from crucible.ports.chat.types import IncomingMessage
from crucible.ports.entitlement import (
    Entitlement,
    EntitlementSource,
    ProfileNarrowing,
    shared_entitlement,
)
from crucible.tools.wiring import ToolWiring


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
        return self._narrow(
            profile, tools=entitlement.tools, instructions=entitlement.instructions, env=env
        )


class GatewayEntitlements:
    """An ``EntitlementSource`` from the agent's own profile: what
    ``runtime.tools_by_gateway`` says a conversation on the message's gateway
    may use (``AgentSpec.tools_on``), and nothing else — no instructions, no
    view of who the person is. Given an ``inner`` source (an application's own
    answer about the person or the place), the two are joined as several
    people's answers are: the tools both allow, the inner's instructions."""

    def __init__(self, spec_of: Callable[[str], AgentSpec | None], *, inner: EntitlementSource | None = None) -> None:
        self._spec_of = spec_of
        self._inner = inner

    async def entitlement(self, agent: str, msg: IncomingMessage) -> Entitlement:
        spec = self._spec_of(agent)
        own = Entitlement(tools=_tools_on(spec, msg.gateway) if spec is not None else None)
        if self._inner is None:
            return own
        theirs = await self._inner.entitlement(agent, msg)
        joined = shared_entitlement([own, theirs])
        return Entitlement(tools=joined.tools, instructions=theirs.instructions)


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
