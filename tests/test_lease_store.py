"""Named leases in the store: one holder across every process that shares it."""

import pytest

from crucible.store import clock
from tests.conftest import Store


async def test_a_lease_is_taken_once_renewed_by_its_holder_and_refused_to_others(store: Store) -> None:
    assert await store.acquire_lease("gateway:slack:assistant", "pod-a:1", ttl_s=30) is True
    assert await store.acquire_lease("gateway:slack:assistant", "pod-b:2", ttl_s=30) is False
    # The holder renews by taking it again; nothing else has to be said.
    assert await store.acquire_lease("gateway:slack:assistant", "pod-a:1", ttl_s=30) is True
    # Another name is another lease.
    assert await store.acquire_lease("gateway:slack:scribe", "pod-b:2", ttl_s=30) is True


async def test_a_lapsed_lease_is_taken_over_and_a_released_one_is_free(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(clock, "now_iso", lambda: "2026-10-23T10:00:00+00:00")
    assert await store.acquire_lease("gateway:slack:assistant", "pod-a:1", ttl_s=30) is True
    monkeypatch.setattr(clock, "now_iso", lambda: "2026-10-23T10:00:20+00:00")
    assert await store.acquire_lease("gateway:slack:assistant", "pod-b:2", ttl_s=30) is False  # still held
    monkeypatch.setattr(clock, "now_iso", lambda: "2026-10-23T10:00:31+00:00")
    assert await store.acquire_lease("gateway:slack:assistant", "pod-b:2", ttl_s=30) is True  # lapsed: taken over
    assert await store.acquire_lease("gateway:slack:assistant", "pod-a:1", ttl_s=30) is False  # the old holder is out

    await store.release_lease("gateway:slack:assistant", "pod-a:1")  # not theirs: left alone
    assert await store.acquire_lease("gateway:slack:assistant", "pod-a:1", ttl_s=30) is False
    await store.release_lease("gateway:slack:assistant", "pod-b:2")
    assert await store.acquire_lease("gateway:slack:assistant", "pod-a:1", ttl_s=30) is True
