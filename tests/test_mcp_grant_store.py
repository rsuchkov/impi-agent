"""What every store of the MCP authorization server's records must do: the
single-use code, the conditional rotation, the lookup by either refresh token.
Run against each backend this environment can reach."""

import sqlite3
from collections.abc import AsyncIterator
from dataclasses import replace
from pathlib import Path

import pytest

from crucible.ports.mcp_grants import McpAuthRequest, McpGrant
from tests.conftest import BACKENDS, backend_for

T0 = "2026-10-10T12:00:00+00:00"
T1 = "2026-10-10T12:10:00+00:00"
T2 = "2026-10-11T00:00:00+00:00"


def _request(**over) -> McpAuthRequest:
    base = McpAuthRequest(
        id="req-1", client="ide", redirect_uri="http://127.0.0.1:5000/cb", code_challenge="c" * 43, scope="read",
        resource="https://engine.example/mcp", state="s1", expires_at=T1, secret="csrf-1",
    )
    return replace(base, **over)


def _grant(**over) -> McpGrant:
    base = McpGrant(
        id="g-1", subject="okta:7", username="alice", client="ide", scope="read act", signed_in_at=T0,
        expires_at=T2, access_hash="a1", access_expires_at=T1, refresh_hash="r1",
    )
    return replace(base, **over)


@pytest.fixture(params=BACKENDS)
async def grants(request: pytest.FixtureRequest, tmp_path: Path) -> AsyncIterator:
    backend = backend_for(request.param, tmp_path)
    store = backend.open()
    try:
        yield store
    finally:
        await store.close()
        backend.cleanup()


async def test_a_request_is_kept_replaced_and_taken_once_by_its_code(grants) -> None:
    await grants.put_mcp_request(_request())
    assert await grants.get_mcp_request("req-1") == _request()
    assert await grants.get_mcp_request("req-9") is None
    consented = _request(subject="okta:7", username="alice", code_hash="h1", code_expires_at=T1)
    await grants.put_mcp_request(consented)  # the same id, rewritten
    assert await grants.get_mcp_request("req-1") == consented
    assert await grants.take_mcp_request_by_code("") is None  # a request without a code is never taken by one
    assert await grants.take_mcp_request_by_code("h1") == consented
    assert await grants.take_mcp_request_by_code("h1") is None  # single use
    assert await grants.get_mcp_request("req-1") is None
    await grants.put_mcp_request(_request(id="req-2"))
    await grants.delete_mcp_request("req-2")
    assert await grants.get_mcp_request("req-2") is None


async def test_a_grant_is_found_by_either_token_and_rotates_once(grants) -> None:
    await grants.save_mcp_grant(_grant())
    assert await grants.get_mcp_grant_by_access("a1") == _grant()
    assert await grants.get_mcp_grant_by_access("nope") is None
    assert await grants.get_mcp_grant_by_refresh("r1") == _grant()
    rotated = _grant(access_hash="a2", access_expires_at=T2, refresh_hash="r2", previous_refresh_hash="r1")
    assert await grants.replace_mcp_grant(rotated, expected_refresh_hash="r1") is True
    # The loser of a race presents the token that was just rotated away.
    assert await grants.replace_mcp_grant(_grant(access_hash="a3", refresh_hash="r3"), expected_refresh_hash="r1") is False
    assert await grants.get_mcp_grant_by_access("a1") is None
    assert await grants.get_mcp_grant_by_access("a2") == rotated
    assert await grants.get_mcp_grant_by_refresh("r1") == rotated  # the previous one still names the grant: a reuse
    assert await grants.get_mcp_grant_by_refresh("r2") == rotated
    assert await grants.get_mcp_grant_by_refresh("") is None
    # A person's grants, oldest sign-in first — and only theirs.
    await grants.save_mcp_grant(_grant(id="g-2", access_hash="a9", refresh_hash="r9", signed_in_at="2026-10-09T12:00:00+00:00"))
    await grants.save_mcp_grant(_grant(id="g-3", subject="okta:8", access_hash="a8", refresh_hash="r8"))
    assert [g.id for g in await grants.mcp_grants_of("okta:7")] == ["g-2", "g-1"]
    assert [g.id for g in await grants.mcp_grants_of("nobody")] == []
    await grants.delete_mcp_grant("g-1")
    assert await grants.get_mcp_grant_by_refresh("r2") is None


async def test_purge_forgets_what_is_past_its_time(grants) -> None:
    await grants.put_mcp_request(_request(id="old", expires_at=T0))
    await grants.put_mcp_request(_request(id="new", expires_at=T2))
    await grants.save_mcp_grant(_grant(id="old-g", access_hash="x", refresh_hash="y", expires_at=T0))
    await grants.save_mcp_grant(_grant(id="new-g"))
    assert await grants.purge_mcp_auth(T1) == 2
    assert await grants.get_mcp_request("old") is None and await grants.get_mcp_request("new") is not None
    assert await grants.get_mcp_grant_by_access("x") is None and await grants.get_mcp_grant_by_access("a1") is not None


async def test_a_grant_table_from_before_refreshed_at_gets_the_column_on_open(tmp_path: Path) -> None:
    """A deployment's SQLite file made by an earlier version lacks the column;
    opening the store adds it, and a grant reads back whole."""
    backend = backend_for("sqlite", tmp_path)
    await backend.open().close()
    conn = sqlite3.connect(tmp_path / "db.sqlite")
    conn.execute("ALTER TABLE mcp_grants DROP COLUMN refreshed_at")
    conn.commit()
    conn.close()
    store = backend.open()
    try:
        await store.save_mcp_grant(_grant(refreshed_at="2026-10-09T12:00:00+00:00"))
        got = await store.get_mcp_grant_by_refresh("r1")
        assert got is not None and got.refreshed_at == "2026-10-09T12:00:00+00:00"
    finally:
        await store.close()
        backend.cleanup()
