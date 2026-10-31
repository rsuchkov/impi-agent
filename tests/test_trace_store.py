"""A turn's tool calls, asked of every backend (crucible/store/base.py).

The widget that shows them is clicked minutes or a restart after the turn, so
the trace has to be there whatever holds it, and gone once nobody would open it.
"""

import sqlite3

from crucible.ports.traces import TraceRecord
from crucible.store.base import Store
from crucible.store.sqlite import SqliteSessionStore
from tests.conftest import StoreBackend

T0 = "2026-09-17T09:00:00+00:00"
T1 = "2026-09-17T09:00:30+00:00"
T2 = "2026-09-17T09:15:00+00:00"


def _trace(**over) -> TraceRecord:
    base = dict(
        token="tr_1", agent="assistant", channel_id="ch1", conversation_id="dm1",
        kind="dm", created_at=T0, finished_at=T1,
        calls='[{"i":0,"tool":"bash","args":"{\\"command\\":\\"make test\\"}"}]',
        post_id="p-42", owner="portal/u1",
    )
    base.update(over)
    return TraceRecord(**base)  # type: ignore[arg-type]


async def test_a_trace_round_trips_with_every_field(store: Store) -> None:
    await store.create_trace(_trace())
    assert await store.get_trace("tr_1") == _trace()


async def test_an_unknown_token_is_none_not_an_error(store: Store) -> None:
    # A pruned or never-written trace is the ordinary case for a click on an
    # old message; the widget answers with an expired card, not a crash.
    assert await store.get_trace("tr_nope") is None


async def test_a_trace_survives_reopen(stores: StoreBackend) -> None:
    # Restarting the engine must not turn every widget into an expired one.
    first = stores.open()
    await first.create_trace(_trace())
    await first.close()

    reopened = stores.open()
    try:
        found = await reopened.get_trace("tr_1")
        assert found is not None and found.post_id == "p-42"
    finally:
        await reopened.close()


async def test_pruning_takes_only_what_finished_before_the_cutoff(store: Store) -> None:
    await store.create_trace(_trace(token="tr_old", finished_at=T0))
    await store.create_trace(_trace(token="tr_new", finished_at=T2))

    assert await store.prune_traces(before=T1) == 1

    assert await store.get_trace("tr_old") is None
    assert await store.get_trace("tr_new") is not None
    assert await store.prune_traces(before=T1) == 0  # nothing left to take


async def test_a_table_from_before_owners_gains_the_column_on_open(tmp_path) -> None:
    # An engine older than the owner column made the table without it; the
    # facet adds the column on open, and a record with an owner round-trips.
    path = tmp_path / "old.sqlite"
    with sqlite3.connect(path) as conn:
        conn.execute(
            "CREATE TABLE tool_traces (token TEXT PRIMARY KEY, agent TEXT NOT NULL, channel_id TEXT NOT NULL,"
            " conversation_id TEXT NOT NULL, kind TEXT NOT NULL, created_at TEXT NOT NULL,"
            " finished_at TEXT NOT NULL, calls TEXT NOT NULL, post_id TEXT NOT NULL DEFAULT '')"
        )
        conn.execute(
            "INSERT INTO tool_traces VALUES ('tr_old', 'assistant', 'ch1', 'dm1', 'dm', ?, ?, '[]', '')", (T0, T1)
        )
    store = SqliteSessionStore(path)
    try:
        old = await store.get_trace("tr_old")
        assert old is not None and old.owner == ""
        await store.create_trace(_trace())
        assert await store.get_trace("tr_1") == _trace()
    finally:
        await store.close()
