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
"""

from crucible.ports.agent.runtime import AgentProfile
from crucible.ports.entitlement import Entitlement, ProfileNarrowing
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
