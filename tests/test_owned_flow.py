"""A turn runs on the replica that owns the conversation; the others forward."""

from pathlib import Path

from crucible.flows.owned import OwnedFlow
from crucible.ports.chat.flow import TurnOutcome
from crucible.ports.chat.types import KIND_DM, ConversationRef, IncomingMessage
from crucible.ports.replica import ConversationOwner, Replica
from tests.fakes.fake_chat import FakeChat

ME = Replica("a1", "http://pod-a:8428")


class _Replicas:
    """A registry with one conversation and a scripted owner."""

    def __init__(self, owner: ConversationOwner | None = None) -> None:
        self.owner = owner  # None = free
        self.claims: list[tuple[str, str, str, bool]] = []

    async def claim_conversation(self, agent, conversation, replica, *, take=False):
        self.claims.append((agent, conversation, replica, take))
        if self.owner is None or take or self.owner.replica == replica:
            self.owner = ConversationOwner(replica)
        return self.owner

    async def replica_url(self, replica):
        return self.owner.url if self.owner and self.owner.replica == replica else None


class _Forwarder:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.sent: list[tuple[str, str, list[IncomingMessage]]] = []

    async def forward_turn(self, url, agent, msgs):
        self.sent.append((url, agent, msgs))
        if self.fail:
            raise ConnectionError("no route to host")
        return TurnOutcome.ACTED


class _Flow:
    def __init__(self) -> None:
        self.batches: list[list[IncomingMessage]] = []

    async def handle_batch(self, msgs, chat):
        self.batches.append(msgs)
        return TurnOutcome.REPLIED


def _msg(gateway: str = "slack", post: str = "p1") -> IncomingMessage:
    return IncomingMessage(
        ref=ConversationRef(channel_id="C1", conversation_id="C1", message_id=post),
        text="hi", user_id="u1", kind=KIND_DM, is_dm=True, gateway=gateway,
    )


def _owned(flow, replicas, forwarder) -> OwnedFlow:
    return OwnedFlow(flow, replicas, ME, forwarder, agent="assistant", forwarded={"mattermost", "slack"})


async def test_a_free_or_own_conversation_runs_here(tmp_path: Path) -> None:
    flow, replicas, forwarder = _Flow(), _Replicas(), _Forwarder()
    owned = _owned(flow, replicas, forwarder)
    assert await owned.handle_batch([_msg()], FakeChat()) is TurnOutcome.REPLIED
    assert await owned.handle_batch([_msg(post="p2")], FakeChat()) is TurnOutcome.REPLIED
    assert len(flow.batches) == 2 and forwarder.sent == []
    # Claimed by the conversation's key, every turn, without force.
    assert replicas.claims == [("assistant", "C1", "a1", False)] * 2


async def test_another_replicas_conversation_is_forwarded_to_it() -> None:
    flow, forwarder = _Flow(), _Forwarder()
    replicas = _Replicas(ConversationOwner("b2", "http://pod-b:8428"))
    owned = _owned(flow, replicas, forwarder)
    msgs = [_msg(), _msg(post="p2")]
    assert await owned.handle_batch(msgs, FakeChat()) is TurnOutcome.ACTED  # the owner's outcome
    assert forwarder.sent == [("http://pod-b:8428", "assistant", msgs)]
    assert flow.batches == []  # nothing ran here
    assert replicas.owner == ConversationOwner("b2", "http://pod-b:8428")  # still theirs


async def test_a_message_nothing_else_can_answer_takes_the_conversation_over() -> None:
    """A request pinned here (http), a socket open here (ws): the turn runs
    here and the conversation moves with it; the owner's memory is then older
    than the conversation, which the flow's catch-up covers."""
    flow, forwarder = _Flow(), _Forwarder()
    replicas = _Replicas(ConversationOwner("b2", "http://pod-b:8428"))
    owned = _owned(flow, replicas, forwarder)
    assert await owned.handle_batch([_msg(gateway="http")], FakeChat()) is TurnOutcome.REPLIED
    assert replicas.claims == [("assistant", "C1", "a1", True)]
    assert replicas.owner == ConversationOwner("a1") and forwarder.sent == []
    assert len(flow.batches) == 1


async def test_an_owner_that_does_not_answer_is_taken_over_with_the_turn_served_here() -> None:
    flow, forwarder = _Flow(), _Forwarder(fail=True)
    replicas = _Replicas(ConversationOwner("b2", "http://pod-b:8428"))
    owned = _owned(flow, replicas, forwarder)
    assert await owned.handle_batch([_msg()], FakeChat()) is TurnOutcome.REPLIED
    assert len(forwarder.sent) == 1 and len(flow.batches) == 1
    assert replicas.claims[-1] == ("assistant", "C1", "a1", True)
    assert replicas.owner == ConversationOwner("a1")


async def test_an_owner_with_no_address_is_taken_over_rather_than_served_beside() -> None:
    flow, forwarder = _Flow(), _Forwarder()
    replicas = _Replicas(ConversationOwner("b2", ""))
    owned = _owned(flow, replicas, forwarder)
    assert await owned.handle_batch([_msg()], FakeChat()) is TurnOutcome.REPLIED
    assert replicas.claims[-1] == ("assistant", "C1", "a1", True)
    assert replicas.owner == ConversationOwner("a1") and forwarder.sent == []
