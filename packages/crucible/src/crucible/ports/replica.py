"""Replica: one engine process among several that share a store.

A deployment may run the engine more than once — pods behind a balancer, two
processes on one machine. They share the store, and so the inventory of
conversations; what they do not share is the runtime's memory of a
conversation (its session files) and the questions a turn is waiting on (a
card's token lives in the process that posted it). So a conversation has an
**owner**: the replica whose runtime holds its memory and runs its turns. A
message for it that arrives elsewhere — through a gateway whose connection
another replica holds — is forwarded to the owner, and so is a click on a card
the owner posted. Ownership moves only when the owner is gone (its heartbeat
lapsed) or when a gateway cannot forward (a request pinned to this replica):
then the new owner's memory is older than the conversation, and the flow's
catch-up (``RuntimeControl.memory_updated_at``) tells it what it missed.

A replica says it is alive by holding the lease ``replica:<id>`` with its own
address as the holder (``ports.lease.Leases``), renewed by its heartbeat; a
conversation's owner is alive while that lease is. The store implements the
registry (``store.base.ReplicaStore``); the forwarding is the http gateway's
(``gateways.http.internal``). A token this replica mints for a click carries
its id in front, so whichever replica the click reaches knows where the
question is.
"""

from dataclasses import dataclass
from enum import Enum, auto
from typing import Protocol

from crucible.ports.chat.flow import TurnOutcome
from crucible.ports.chat.types import IncomingMessage

# The lease a live replica holds, named by its id; the holder is its address.
REPLICA_LEASE_PREFIX = "replica:"
# Between a replica's id and the token proper in a tagged click token.
TOKEN_TAG_SEP = "."


@dataclass(frozen=True)
class Replica:
    """This process, as the others can reach it."""

    id: str  # short, unique among the replicas sharing the store
    url: str = ""  # where its hub answers; "" when nothing can be forwarded to it


@dataclass(frozen=True)
class ConversationOwner:
    """Who serves a conversation now: a replica's id and address."""

    replica: str
    url: str = ""


class Replicas(Protocol):
    """The registry the store keeps: which replica owns which conversation,
    and where the live ones are."""

    async def claim_conversation(
        self, agent: str, conversation: str, replica: str, *, take: bool = False
    ) -> ConversationOwner:
        """Make ``replica`` the owner of the conversation when it is free, lapsed
        (its owner's heartbeat gone) or already its own — or whatever the state
        when ``take`` is set, for a message that cannot be forwarded — and say
        who the owner is afterwards: ``replica`` itself, or the live holder."""
        ...

    async def replica_url(self, replica: str) -> str | None:
        """Where a live replica answers, or None when it is gone or unknown."""
        ...


class TurnForwarder(Protocol):
    """Hands a batch of messages to the replica that owns their conversation,
    and waits for the turn's outcome."""

    async def forward_turn(self, url: str, agent: str, msgs: list[IncomingMessage]) -> TurnOutcome: ...


class ClickKind(Enum):
    APPROVAL = auto()  # a request for authorization: a gated tool call, a credential
    PENDING = auto()  # a blocking question the turn waits on


class ForwardedClick(Enum):
    """What the replica that was asked did with a forwarded click."""

    RESOLVED = auto()
    NOT_ALLOWED = auto()  # a live request, but this person may not answer it
    NOT_MINE = auto()  # nothing waits for this token there either


class ClickForwarder(Protocol):
    async def forward_click(
        self, url: str, kind: ClickKind, token: str, value: str, user_id: str, gateway: str
    ) -> ForwardedClick: ...


def tag_token(replica: str, token: str) -> str:
    """A token that says which replica minted it (none when ``replica`` is "")."""
    return f"{replica}{TOKEN_TAG_SEP}{token}" if replica else token


def replica_of(token: str) -> str:
    """The replica a tagged token names, "" for an untagged one."""
    tag, sep, _ = token.partition(TOKEN_TAG_SEP)
    return tag if sep else ""
