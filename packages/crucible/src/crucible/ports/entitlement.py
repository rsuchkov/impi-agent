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

from collections.abc import Collection, Mapping, Sequence
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
    something a person typed. ``deferred``: names among ``tools`` the model is
    not told about until it asks — their declarations are left out of every
    request, and the runtime's own search tool loads one when the model looks
    for what it does. For a conversation with many tools and a model with a
    small window; ``None`` or empty declares everything, as always. Which
    tools can be deferred is the runtime's: the engine's typed tools and MCP
    tools can, a runtime's built-ins cannot, and a built-in named here is
    declared as it is. ``agents``: the names of the agents this conversation
    may hand a task to (``ask_agent``, ``delegate``) — the agent's own profile
    (``trusted_agents``, the deployment's roster) is the ceiling, a name
    outside it is ignored; ``None`` leaves the profile to decide, ``()`` lets
    the conversation ask nobody. An agent the conversation may not ask is not
    there for it: the tools list the others, and asking is answered as asking
    an agent that does not run here. For an agent of a team — its own words,
    knowledge and model, for the people of the team — to answer only them,
    whoever the asker asks for.
    """

    tools: Collection[str] | None = None
    instructions: str = ""
    deferred: Collection[str] | None = None
    agents: Collection[str] | None = None


def shared_entitlement(entitlements: Sequence[Entitlement]) -> Entitlement:
    """What several people in one turn are jointly entitled to — a batch of
    messages in a channel or a thread may come from more than one person, and
    the turn runs once for all of them. Tools: only what every one of them
    may use (an answer of None imposes nothing; one answer of ``()`` leaves
    none). Instructions: only when every answer says the same, since words the
    application vouches for about one person cannot stand for another.
    Deferred: what any answer defers — a declaration held back for one person
    is held back for the turn, since the model that reads it is one. Agents:
    as tools — only the ones every one of them may ask."""
    if not entitlements:
        return Entitlement()
    ordered = _everyones(tuple(e.tools for e in entitlements))
    agents = _everyones(tuple(e.agents for e in entitlements))
    instructions = {e.instructions for e in entitlements}
    deferred: tuple[str, ...] | None = None
    if any(e.deferred for e in entitlements):
        deferred = tuple(dict.fromkeys(name for e in entitlements for name in (e.deferred or ())))
    return Entitlement(
        tools=ordered,
        instructions=next(iter(instructions)) if len(instructions) == 1 else "",
        deferred=deferred,
        agents=agents,
    )


def _everyones(answers: Sequence[Collection[str] | None]) -> Collection[str] | None:
    """The names every answer allows (an answer of None imposes nothing; one
    answer of ``()`` leaves none), in the order the first restricting answer
    gave them; None when no answer restricts."""
    allowed: set[str] | None = None
    for names in answers:
        if names is None:
            continue
        allowed = set(names) if allowed is None else allowed & set(names)
    if allowed is None:
        return None
    first = next(names for names in answers if names is not None)
    return tuple(dict.fromkeys(n for n in first if n in allowed))


class EntitlementSource(Protocol):
    """Where a flow asks what a conversation may do: answered from the message
    that opens the turn (who sent it, in which conversation), before the
    turn runs. The answer may differ from one turn to the next; the engine
    follows it."""

    async def entitlement(
        self, agent: str, msg: IncomingMessage, *, tools: Collection[str] | None = None
    ) -> Entitlement:
        """``tools``: the ceiling another rule already set for this conversation
        — the gateway's list, when the agent narrows one — so instructions
        that speak of tools speak of the ones that will be there. None: no
        ceiling but the agent's own."""
        ...


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
    the agent's), the instructions to append, the tool env the conversation's
    process gets (its manifest among it; None when tools are off) and the
    names whose declaration is deferred (None = none), give back the profile
    for its process. Each runtime driver has one; the neutral
    ``crucible.conversation_profiles`` calls it and never looks inside."""

    def __call__(
        self,
        profile: AgentProfile,
        *,
        tools: Collection[str] | None,
        instructions: str,
        env: Mapping[str, str] | None,
        deferred: Collection[str] | None = None,
    ) -> AgentProfile: ...
