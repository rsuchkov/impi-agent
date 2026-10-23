"""AgentUnit: one agent's live presence on a platform.

A small composition-level record shared by the app wiring and the profile
reloader. It lives in its own module so neither has to import the other.
"""

from dataclasses import dataclass

from crucible.flows.agent_flow import AgentFlow
from crucible.ports.agent import AgentSpec
from crucible.ports.chat.gateway import Gateway


@dataclass
class AgentUnit:
    """Each gateway owns its driver and chat client; they are not part of the
    manifest. ``spec`` and ``flow`` are reassigned in place on a hot-reload."""

    spec: AgentSpec
    flow: AgentFlow
    # The gateways the agent is reached through, by kind; the first is the default.
    gateways: dict[str, Gateway]
