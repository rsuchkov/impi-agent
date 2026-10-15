"""What one conversation may do, as distinct from what its agent may do.

An agent's profile says which tools it has and what its system prompt is, once,
for everyone who talks to it. An application may know more about a particular
conversation: the person on the other side belongs to a group that was given
some of those tools and not others, or the conversation runs inside a system
the agent should be told about in words the model must trust. ``Entitlement``
is that knowledge in neutral terms — a subset of the agent's tool names and a
paragraph for its system prompt — and an ``EntitlementSource`` is where the
flow asks for it, with the message that opens the turn, every turn. Without a
source every conversation is entitled to the agent's whole profile, which is
how the engine has always behaved.

The flow then hands the agent's profile and the entitlement to a
``ConversationProfiles``, which builds the profile this conversation's process
runs with. What that profile looks like is the runtime's business: the neutral
``crucible.conversation_profiles`` keeps the tool server in step and asks the
runtime driver's ``ProfileNarrowing`` for the profile itself; the flow only
passes it on. The runtime restarts a conversation's process when the
profile it is handed is not the one the process was started with — its memory
is on disk, so the conversation goes on — and the tool server refuses a call
for a tool outside the conversation's set whatever the process claims.
"""

from collections.abc import Collection, Mapping
from dataclasses import dataclass
from typing import Protocol

from crucible.ports.agent.runtime import AgentProfile
from crucible.ports.chat.types import IncomingMessage


@dataclass(frozen=True)
class Entitlement:
    """What a conversation is allowed, out of its agent's profile.

    ``tools``: the names this conversation may use, built-ins and typed tools
    alike (the runtime's allowlist is one list) — the agent's own list is the
    ceiling, a name outside it is ignored; ``None`` leaves the agent's list as
    it is, ``()`` leaves no tool at all. ``instructions``: text the application
    vouches for, appended to the system prompt — trusted, unlike anything
    quoted in the prompt, so it must be the application's own words and never
    something a person typed.
    """

    tools: Collection[str] | None = None
    instructions: str = ""

    @property
    def is_default(self) -> bool:
        """Whether this entitlement changes nothing about the agent's profile."""
        return self.tools is None and not self.instructions


class EntitlementSource(Protocol):
    """Where a flow asks what a conversation may do: answered from the message
    that opens the turn (who sent it, in which conversation), before the
    turn runs. The answer may differ from one turn to the next; the engine
    follows it."""

    async def entitlement(self, agent: str, msg: IncomingMessage) -> Entitlement: ...


class ConversationProfiles(Protocol):
    """Builds the profile a conversation's process runs with: the agent's
    profile, narrowed and extended by an entitlement, for one runtime session.
    The same inputs give the same profile, so a runtime can tell by comparing
    profiles whether the process it holds is the one this turn needs."""

    def build(
        self, agent: str, profile: AgentProfile, entitlement: Entitlement, runtime_session_id: str
    ) -> AgentProfile: ...


class ProfileNarrowing(Protocol):
    """The runtime's half of building a conversation's profile: given the
    agent's profile, the tool names the conversation may use (None = all of
    the agent's), the instructions to append and the tool env the conversation's
    process gets (its manifest among it; None when tools are off), give back
    the profile for its process. Each runtime driver has one; the neutral
    ``crucible.conversation_profiles`` calls it and never looks inside."""

    def __call__(
        self,
        profile: AgentProfile,
        *,
        tools: Collection[str] | None,
        instructions: str,
        env: Mapping[str, str] | None,
    ) -> AgentProfile: ...
