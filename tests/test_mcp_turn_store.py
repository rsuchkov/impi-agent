"""What every store of the MCP gateway's turns must do: the newest turn of a
conversation, the same request again, a cancel from another replica, a
replica's turns closed when it restarts. Run against each backend this
environment can reach, and the in-memory store beside them."""

from collections.abc import AsyncIterator
from dataclasses import replace
from pathlib import Path

import pytest

from crucible.ports.mcp_turns import (
    TURN_DONE,
    TURN_FAILED,
    TURN_WORKING,
    McpTurnRecord,
)
from tests.conftest import BACKENDS, backend_for
from tests.fakes.mcp_turns import MemoryMcpTurns

T0 = "2026-10-10T12:00:00+00:00"
T1 = "2026-10-10T12:00:30+00:00"
T2 = "2026-10-10T12:01:00+00:00"
T9 = "2026-10-11T00:00:00+00:00"


def _turn(**over) -> McpTurnRecord:
    base = McpTurnRecord(
        turn_id="t1", conversation="mcp:alice:h1", handle="h1", subject="alice", client="ide", agent="helper",
        replica="pod-a", state=TURN_WORKING, started_at=T0, updated_at=T0, expires_at=T9, step="starting",
        request_digest="d1",
    )
    return replace(base, **over)


@pytest.fixture(params=["memory", *BACKENDS])
async def turns(request: pytest.FixtureRequest, tmp_path: Path) -> AsyncIterator:
    if request.param == "memory":
        yield MemoryMcpTurns()
        return
    backend = backend_for(request.param, tmp_path)
    store = backend.open()
    try:
        yield store
    finally:
        await store.close()
        backend.cleanup()


async def test_a_turn_is_kept_rewritten_and_read_as_its_conversations_newest(turns) -> None:
    await turns.save_mcp_turn(_turn())
    assert await turns.get_mcp_turn("t1") == _turn()
    assert await turns.get_mcp_turn("nope") is None
    done = _turn(state=TURN_DONE, step="", answer="42", updated_at=T1)
    await turns.save_mcp_turn(done)
    assert await turns.get_mcp_turn("t1") == done
    await turns.save_mcp_turn(_turn(turn_id="t2", started_at=T2, updated_at=T2, request_digest="d2"))
    latest = await turns.latest_mcp_turn("mcp:alice:h1")
    assert latest is not None and latest.turn_id == "t2"
    assert await turns.latest_mcp_turn("mcp:alice:other") is None


async def test_the_same_request_again_finds_the_turn_still_running(turns) -> None:
    await turns.save_mcp_turn(_turn())
    found = await turns.find_mcp_turn("alice", "ide", "d1")
    assert found is not None and found.turn_id == "t1"
    assert await turns.find_mcp_turn("alice", "other-client", "d1") is None  # another client's
    assert await turns.find_mcp_turn("alice", "ide", "") is None  # no digest names nothing
    await turns.save_mcp_turn(_turn(state=TURN_DONE))
    assert await turns.find_mcp_turn("alice", "ide", "d1") is None  # ended: a new request is a new turn


async def test_a_cancel_is_a_mark_any_replica_reads(turns) -> None:
    await turns.save_mcp_turn(_turn())
    assert await turns.request_mcp_cancel("t1") is True
    assert await turns.request_mcp_cancel("nope") is False
    marked = await turns.get_mcp_turn("t1")
    assert marked is not None and marked.cancel_requested is True and marked.state == TURN_WORKING
    # The replica running the turn writes from its own copy, which says no
    # cancel: a save leaves the mark as the other replica set it.
    await turns.save_mcp_turn(_turn(step="searching", updated_at=T1))
    kept = await turns.get_mcp_turn("t1")
    assert kept is not None and kept.cancel_requested is True and kept.step == "searching"
    await turns.save_mcp_turn(_turn(turn_id="t2", cancel_requested=True))  # a new row keeps what it is given
    fresh = await turns.get_mcp_turn("t2")
    assert fresh is not None and fresh.cancel_requested is True


async def test_a_restarted_replicas_turns_are_closed_and_old_records_purged(turns) -> None:
    await turns.save_mcp_turn(_turn())
    await turns.save_mcp_turn(_turn(turn_id="t2", replica="pod-b"))
    await turns.save_mcp_turn(_turn(turn_id="t3", state=TURN_DONE, answer="ok"))
    assert await turns.fail_mcp_turns("pod-a", "restarted", T2, T9) == 1  # t1 alone: t3 had ended
    failed = await turns.get_mcp_turn("t1")
    assert failed is not None and failed.state == TURN_FAILED and failed.answer == "restarted" and failed.is_error
    assert failed.updated_at == T2 and failed.expires_at == T9 and failed.step == ""
    other = await turns.get_mcp_turn("t2")
    assert other is not None and other.state == TURN_WORKING
    await turns.save_mcp_turn(_turn(turn_id="old", expires_at=T1))
    assert await turns.purge_mcp_turns(T2) == 1
    assert await turns.get_mcp_turn("old") is None and await turns.get_mcp_turn("t1") is not None
