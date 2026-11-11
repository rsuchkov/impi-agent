"""ConversationKey and PersonKey: which conversation a message belongs to, and
who a platform id is.

The engine keeps one runtime session per conversation, under a key. A platform
gives every message a conversation id of its own — a thread, a direct message, a
caller's dialog — and for one gateway that id is the key. An agent reached
through several gateways is a different matter: the same person's task thread in
one chat and their dialog in a web panel are one conversation to them, and only
the application knows how the two ids relate. It says so here; the engine then
runs both through one session, and the memory follows the person, not the door.
The same person also carries a different id on each gateway; ``PersonKey`` says
which ids are one person, for the one decision the engine makes about people —
who may answer a request for authorization.
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
    conversation. The default."""

    def key(self, agent: str, msg: IncomingMessage) -> str:
        return msg.conversation_id


class AuthorKey:
    """A channel's conversation — the channel, a thread in it — is the
    author's own: keyed by the platform's id and the author, so each person
    who speaks to the agent there has a session of their own, and what the
    model learned with one person's rights stays with that person; a direct
    message (and a thread answering one) is the person's already and keeps the
    platform's id. The turn still answers in the thread, where the message
    is, and the thread's replies — everyone's — reach the session as context
    the way they always have. Nobody's message (no author) keeps the
    platform's id too."""

    def key(self, agent: str, msg: IncomingMessage) -> str:
        if msg.direct or not msg.user_id:
            return msg.conversation_id
        return f"{msg.conversation_id}:{msg.user_id}"


class PersonKey(Protocol):
    """Who a platform id is, across gateways. The engine compares people in
    one place — who may answer a request for authorization — and one person
    may reach an agent through several gateways under several ids (a Slack
    id, a caller's user id). The application knows which ids are one person;
    this says so. Deterministic, and the identity for an id it does not know."""

    def key(self, gateway: str, user_id: str) -> str: ...


class PlatformPerson:
    """The platform's own id is the person — one gateway, one id. The default."""

    def key(self, gateway: str, user_id: str) -> str:
        return user_id
