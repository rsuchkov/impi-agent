"""Gateway port: one agent's connection to one chat platform."""

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class AgentIdentity:
    """The agent account's identity on the platform, learned at login. The
    gateway is the sole authority on this — the neutral layers never derive it
    from a platform-specific profile field."""

    user_id: str
    username: str


# The kinds of gateway an agent may be reached through — the names the
# configuration uses, and what ``IncomingMessage.gateway`` says a message came
# through. One agent may have several: a person reaches the same agent, the same
# conversation, from whichever of them they are in.
GATEWAY_MATTERMOST = "mattermost"
GATEWAY_SLACK = "slack"
GATEWAY_WS = "ws"
GATEWAY_HTTP = "http"
GATEWAY_KINDS = (GATEWAY_MATTERMOST, GATEWAY_SLACK, GATEWAY_WS, GATEWAY_HTTP)
# The engine's own door: a turn one agent opens for another on a task
# (``ports.delegation``). Not a kind an operator configures — every agent has
# it — but a conversation can be live in it, and a message can say it came
# through it, like through any gateway.
GATEWAY_DELEGATION = "delegation"
# The same door for an agent the asker trusts (``AgentSpec.trusted_agents``): a
# turn there runs as the person, with the asker's secrets and connections it was
# told to hand over, and its cards go to the asker's conversation.
GATEWAY_TRUSTED_DELEGATION = "trusted-delegation"

# Where a chat gateway answers a top-level direct message: at the top of the DM,
# or in a thread under the person's message. The conversation is the DM either way.
DM_REPLIES_TOP = "top"
DM_REPLIES_THREAD = "thread"
DM_REPLIES = (DM_REPLIES_TOP, DM_REPLIES_THREAD)


class Gateway(Protocol):
    """Lifecycle: login() once, then run() until stop()."""

    async def login(self) -> AgentIdentity:
        """Authenticate and return the agent account's platform identity."""
        ...

    async def run(self) -> None:
        """Consume platform events until stop(); returns when disconnected."""
        ...

    async def stop(self) -> None: ...
