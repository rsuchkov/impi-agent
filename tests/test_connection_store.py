"""What every store of connected accounts must do: the lease, the conditional
writes, the single-use link. Run against each backend."""

from collections.abc import AsyncIterator
from dataclasses import replace

import pytest

from crucible.connections.memory import MemoryConnectionStore, MemoryLinkStore
from crucible.store.base import ConnectionLinkRecord, ConnectionRecord

T0 = "2026-10-10T12:00:00+00:00"
T1 = "2026-10-10T12:00:30+00:00"
T2 = "2026-10-10T12:01:00+00:00"


def _record(**over) -> ConnectionRecord:
    base = ConnectionRecord(
        owner="mattermost:u1", provider="gitlab", access_sealed="A1", refresh_sealed="R1",
        expires_at=T2, account_id="7", account_username="alice", account_name="Alice",
        account_email="alice@example.com", scope="read_api", connect_id="g1",
    )
    return replace(base, **over)


@pytest.fixture(params=["memory"])
async def connections(request: pytest.FixtureRequest) -> AsyncIterator[MemoryConnectionStore]:
    yield MemoryConnectionStore()


@pytest.fixture(params=["memory"])
async def links(request: pytest.FixtureRequest) -> AsyncIterator[MemoryLinkStore]:
    yield MemoryLinkStore()


async def test_a_connection_is_kept_per_owner_and_provider(connections) -> None:
    await connections.save_connection(_record())
    await connections.save_connection(_record(provider="jira", connect_id="g9"))
    await connections.save_connection(_record(owner="slack:U2"))
    assert await connections.get_connection("mattermost:u1", "gitlab") == _record()
    assert [r.provider for r in await connections.list_connections("mattermost:u1")] == ["gitlab", "jira"]
    assert await connections.get_connection("mattermost:u1", "github") is None


async def test_the_lease_is_held_once_and_only_over_the_credential_seen(connections) -> None:
    await connections.save_connection(_record())
    claim = dict(connect_id="g1", version=0, now=T0, until=T1)
    assert await connections.claim_refresh("mattermost:u1", "gitlab", **claim)
    assert not await connections.claim_refresh("mattermost:u1", "gitlab", **claim)  # held
    # Only the holder's credential ends it early.
    await connections.release_refresh("mattermost:u1", "gitlab", connect_id="g1", version=5)
    assert not await connections.claim_refresh("mattermost:u1", "gitlab", **claim)
    # Lapsed at its end; released early by its holder; never over another credential.
    assert await connections.claim_refresh("mattermost:u1", "gitlab", connect_id="g1", version=0, now=T1, until=T2)
    await connections.release_refresh("mattermost:u1", "gitlab", connect_id="g1", version=0)
    assert await connections.claim_refresh("mattermost:u1", "gitlab", **claim)
    assert not await connections.claim_refresh(
        "mattermost:u1", "gitlab", connect_id="g1", version=5, now=T2, until=T2,
    )
    assert not await connections.claim_refresh("mattermost:u1", "gitlab", connect_id="gX", version=0, now=T2, until=T2)


async def test_a_write_from_a_stale_credential_changes_nothing(connections) -> None:
    await connections.save_connection(_record())
    assert await connections.claim_refresh("mattermost:u1", "gitlab", connect_id="g1", version=0, now=T0, until=T2)
    rotated = _record(access_sealed="A2", version=1)
    assert await connections.replace_connection(rotated, expected_version=0)
    # The replace ended the lease, and the new version can be claimed.
    assert await connections.claim_refresh("mattermost:u1", "gitlab", connect_id="g1", version=1, now=T0, until=T1)
    # A slow refresh that read version 0 cannot write over version 1…
    assert not await connections.replace_connection(_record(access_sealed="stale", version=1), expected_version=0)
    # …nor can an old grant delete a reconnect.
    await connections.save_connection(_record(connect_id="g2"))
    assert not await connections.delete_connection("mattermost:u1", "gitlab", connect_id="g1", version=1)
    assert not await connections.replace_connection(_record(connect_id="g1", version=2), expected_version=1)
    assert (await connections.get_connection("mattermost:u1", "gitlab")).connect_id == "g2"
    assert await connections.delete_connection("mattermost:u1", "gitlab", connect_id="g2", version=0)
    assert not await connections.delete_connection("mattermost:u1", "gitlab")


async def test_saving_a_connection_ends_any_lease(connections) -> None:
    await connections.save_connection(_record())
    assert await connections.claim_refresh("mattermost:u1", "gitlab", connect_id="g1", version=0, now=T0, until=T2)
    await connections.save_connection(_record(connect_id="g2"))
    assert await connections.claim_refresh("mattermost:u1", "gitlab", connect_id="g2", version=0, now=T0, until=T2)


async def test_a_link_can_be_taken_once(links) -> None:
    link = ConnectionLinkRecord(
        state_digest="d1", owner="mattermost:u1", provider="gitlab", verifier_sealed="V", expires_at=T2,
    )
    await links.put_link(link)
    assert await links.take_link("d1") == link
    assert await links.take_link("d1") is None
    assert await links.take_link("nope") is None
