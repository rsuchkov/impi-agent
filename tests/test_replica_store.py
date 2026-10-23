"""Which replica owns a conversation, across every process sharing the store."""

import pytest

from crucible.ports.replica import REPLICA_LEASE_PREFIX, ConversationOwner
from crucible.store import clock
from tests.conftest import Store


async def _alive(store: Store, replica: str, url: str) -> None:
    assert await store.acquire_lease(REPLICA_LEASE_PREFIX + replica, url, ttl_s=30) is True


async def test_a_free_conversation_is_claimed_and_kept_by_its_live_owner(store: Store) -> None:
    await _alive(store, "a1", "http://pod-a:8428")
    await _alive(store, "b2", "http://pod-b:8428")
    assert await store.claim_conversation("assistant", "dm1", "a1") == ConversationOwner("a1")
    # The owner's claim is renewed, not contested; another replica is told who
    # owns it and where that replica answers.
    assert await store.claim_conversation("assistant", "dm1", "a1") == ConversationOwner("a1")
    assert await store.claim_conversation("assistant", "dm1", "b2") == ConversationOwner(
        "a1", "http://pod-a:8428"
    )
    # Another conversation, another agent: other rows.
    assert await store.claim_conversation("assistant", "dm2", "b2") == ConversationOwner("b2")
    assert await store.claim_conversation("scribe", "dm1", "b2") == ConversationOwner("b2")


async def test_a_conversation_whose_owner_lapsed_or_who_is_taken_by_force_moves(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(clock, "now_iso", lambda: "2026-10-24T10:00:00+00:00")
    await _alive(store, "a1", "http://pod-a:8428")
    await _alive(store, "b2", "http://pod-b:8428")
    assert await store.claim_conversation("assistant", "dm1", "a1") == ConversationOwner("a1")
    # Taken by force while the owner lives: a message that cannot be forwarded.
    assert await store.claim_conversation("assistant", "dm1", "b2", take=True) == ConversationOwner("b2")
    assert await store.claim_conversation("assistant", "dm1", "a1") == ConversationOwner(
        "b2", "http://pod-b:8428"
    )
    # The owner stops renewing: once its lease lapsed, the conversation is free.
    monkeypatch.setattr(clock, "now_iso", lambda: "2026-10-24T10:00:20+00:00")
    await _alive(store, "a1", "http://pod-a:8428")  # a1 renews, b2 does not
    assert await store.claim_conversation("assistant", "dm1", "a1") == ConversationOwner(
        "b2", "http://pod-b:8428"
    )
    monkeypatch.setattr(clock, "now_iso", lambda: "2026-10-24T10:00:31+00:00")
    assert await store.claim_conversation("assistant", "dm1", "a1") == ConversationOwner("a1")


async def test_a_replicas_address_is_known_while_its_lease_lives(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(clock, "now_iso", lambda: "2026-10-24T10:00:00+00:00")
    assert await store.replica_url("a1") is None
    await _alive(store, "a1", "http://pod-a:8428")
    assert await store.replica_url("a1") == "http://pod-a:8428"
    monkeypatch.setattr(clock, "now_iso", lambda: "2026-10-24T10:00:31+00:00")
    assert await store.replica_url("a1") is None
    # A conversation owned by a replica nobody can reach is reported with no address.
    monkeypatch.setattr(clock, "now_iso", lambda: "2026-10-24T10:00:00+00:00")
    await store.release_lease(REPLICA_LEASE_PREFIX + "a1", "http://pod-a:8428")
    assert await store.claim_conversation("assistant", "dm1", "a1") == ConversationOwner("a1")
    await store.release_lease(REPLICA_LEASE_PREFIX + "a1", "http://pod-a:8428")
    # ...and is free for the taking.
    assert await store.claim_conversation("assistant", "dm1", "b2") == ConversationOwner("b2")
