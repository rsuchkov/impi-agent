"""OwnedFlow: a turn runs where the conversation lives.

Several replicas share one store (``ports.replica``). A message may reach any
of them — through the gateway connection that replica holds, from its
scheduler — while the conversation's runtime memory and its waiting cards are
on one: the owner. So before a turn, the batch's conversation is claimed: free
or lapsed, it is ours and the flow runs here; another replica's and alive, the
batch goes to that replica over its hub and its outcome comes back. A message
that cannot be served from anywhere else — a request pinned to this replica by
a balancer, a websocket open here — takes the conversation over instead, and
the flow's catch-up tells the runtime what it missed meanwhile.

An owner that cannot be reached although its heartbeat lives is taken over too,
with a warning: a turn served late beats one never served. The flow's own
replay check keeps a message from running twice when the owner did serve it.

A ``Flow`` like the one it wraps, so a coalescer holds it unchanged.
"""

import logging
from collections.abc import Collection

from crucible.ports.chat.client import ChatClient
from crucible.ports.chat.flow import Flow, TurnOutcome
from crucible.ports.chat.keying import ConversationKey, PlatformKey
from crucible.ports.chat.types import IncomingMessage
from crucible.ports.replica import Replica, Replicas, TurnForwarder

logger = logging.getLogger(__name__)


class OwnedFlow:
    def __init__(
        self,
        flow: Flow,
        replicas: Replicas,
        replica: Replica,
        forwarder: TurnForwarder,
        *,
        agent: str,
        keying: ConversationKey | None = None,
        forwarded: Collection[str] = (),
    ) -> None:
        self._flow = flow
        self._replicas = replicas
        self._replica = replica
        self._forwarder = forwarder
        self._agent = agent
        # The same answer the flow keeps its session by.
        self._keying: ConversationKey = keying or PlatformKey()
        # Gateways whose messages any replica can answer (sending needs no
        # connection): those are forwarded. A message through any other gateway
        # is served here, and the conversation moves here with it.
        self._forwarded = frozenset(forwarded)

    async def handle_batch(self, msgs: list[IncomingMessage], chat: ChatClient) -> TurnOutcome:
        anchor = msgs[-1]
        conversation = self._keying.key(self._agent, anchor)
        take = anchor.gateway not in self._forwarded
        owner = await self._replicas.claim_conversation(
            self._agent, conversation, self._replica.id, take=take
        )
        if owner.replica != self._replica.id and owner.url:
            try:
                return await self._forwarder.forward_turn(owner.url, self._agent, msgs)
            except Exception:
                logger.warning(
                    "conversation %s of %s: owner %s at %s did not take the turn; serving it here",
                    conversation, self._agent, owner.replica, owner.url, exc_info=True,
                )
                await self._replicas.claim_conversation(
                    self._agent, conversation, self._replica.id, take=True
                )
        return await self._flow.handle_batch(msgs, chat)
