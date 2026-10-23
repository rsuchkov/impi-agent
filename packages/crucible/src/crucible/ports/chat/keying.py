"""ConversationKey: which conversation a message belongs to.

The engine keeps one runtime session per conversation, under a key. A platform
gives every message a conversation id of its own — a thread, a direct message, a
caller's dialog — and for one gateway that id is the key. An agent reached
through several gateways is a different matter: the same person's task thread in
one chat and their dialog in a web panel are one conversation to them, and only
the application knows how the two ids relate. It says so here; the engine then
runs both through one session, and the memory follows the person, not the door.
"""

from typing import Protocol

from crucible.ports.chat.types import IncomingMessage


class ConversationKey(Protocol):
    def key(self, agent: str, msg: IncomingMessage) -> str:
        """The conversation ``msg`` belongs to, as the key the engine keeps the
        session under. Deterministic: the same message always gives the same
        key, and two messages of one conversation the same key."""
        ...


class PlatformKey:
    """The platform's own conversation id is the key — one gateway, one id per
    conversation. What every deployment had before gateways could be several."""

    def key(self, agent: str, msg: IncomingMessage) -> str:
        return msg.conversation_id
