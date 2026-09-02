"""What is true of the SQLite backend and of nothing else.

The behaviour every backend owes lives in test_session_store.py and its
siblings; this file is for the file itself — the schema it migrates in place,
the pragmas it sets, and the columns an older engine would still read after a
rollback. These assertions reach into `_conn` on purpose: they are about the
mechanism, not about what the store promises.
"""

import sqlite3
from pathlib import Path

from crucible.store.base import FormRecord, SchedulerHeartbeat
from crucible.store.sessions import SqliteSessionStore
from tests.test_approval_store import _audit, _grant
from tests.test_scheduler_store import T0, T1, T2, _run, _task


async def test_migration_adds_last_user_id_to_old_db(tmp_path: Path) -> None:
    # Simulate a pre-ephemeral DB: sessions table WITHOUT last_user_id.
    db = tmp_path / "old.sqlite"
    conn = sqlite3.connect(str(db))
    conn.executescript(
        "CREATE TABLE sessions (id INTEGER PRIMARY KEY, agent TEXT NOT NULL, "
        "channel_id TEXT NOT NULL, conversation_id TEXT NOT NULL, kind TEXT NOT NULL, "
        "runtime_session_id TEXT NOT NULL, created_at TEXT NOT NULL, last_active TEXT NOT NULL, "
        "UNIQUE (agent, conversation_id));"
        "INSERT INTO sessions (agent, channel_id, conversation_id, kind, runtime_session_id, "
        "created_at, last_active) VALUES "
        "('assistant','ch1','root1','thread','assistant--root1','2020-01-01','2020-01-01');"
    )
    conn.commit()
    conn.close()

    store = SqliteSessionStore(db)  # opening runs the migration
    try:
        rec = await store.get_by_runtime_session("assistant--root1")
        assert rec is not None and rec.last_user_id == ""  # backfilled default
        # And the column is writable now.
        await store.touch("assistant", "root1", user_id="u9")
        rec2 = await store.get_by_runtime_session("assistant--root1")
        assert rec2 is not None and rec2.last_user_id == "u9"
    finally:
        await store.close()


async def test_migration_adds_post_id_to_old_pending_forms(tmp_path: Path) -> None:
    # Simulate a DB from before the form button could be retired: pending_forms
    # WITHOUT post_id, holding a form that was already waiting for a click.
    db = tmp_path / "old-forms.sqlite"
    conn = sqlite3.connect(str(db))
    conn.executescript(
        "CREATE TABLE pending_forms (token TEXT PRIMARY KEY, agent TEXT NOT NULL, "
        "channel_id TEXT NOT NULL, conversation_id TEXT NOT NULL, kind TEXT NOT NULL, "
        "spec TEXT NOT NULL, created_at TEXT NOT NULL);"
        "INSERT INTO pending_forms VALUES ('t-old','assistant','ch1','root1','thread',"
        "'{}','2020-01-01');"
    )
    conn.commit()
    conn.close()

    store = SqliteSessionStore(db)  # opening runs the migration
    try:
        old = await store.get_form("t-old")
        assert old is not None and old.post_id == ""  # nothing to retire: no id was recorded
        await store.create_form(
            FormRecord(token="t-new", agent="assistant", channel_id="ch1",
                       conversation_id="root1", kind="thread", spec="{}",
                       created_at="2026-01-01", post_id="p-42")
        )
        fresh = await store.get_form("t-new")
        assert fresh is not None and fresh.post_id == "p-42"  # and the column is writable
    finally:
        await store.close()


async def test_an_older_engine_still_reads_a_migrated_db(tmp_path: Path) -> None:
    """`impi update` offers a rollback, so yesterday's engine must survive today's
    schema. It queries by explicit column lists, so an added column is invisible
    to it — this pins that."""
    db = tmp_path / "new.sqlite"
    store = SqliteSessionStore(db)  # current schema
    await store.create_form(
        FormRecord(token="t1", agent="assistant", channel_id="ch1", conversation_id="root1",
                   kind="thread", spec="{}", created_at="2026-01-01", post_id="p1")
    )
    await store.get_or_create("assistant", "ch1", "root1", "thread", user_id="u1")
    await store.close()

    old_form_cols = "token, agent, channel_id, conversation_id, kind, spec, created_at"
    old_session_cols = ("agent, channel_id, conversation_id, kind, runtime_session_id, "
                        "created_at, last_active")
    conn = sqlite3.connect(str(db))
    try:
        assert len(conn.execute(f"SELECT {old_form_cols} FROM pending_forms").fetchone()) == 7
        assert len(conn.execute(f"SELECT {old_session_cols} FROM sessions").fetchone()) == 7
        # Writing the old way works too: the added columns carry defaults.
        conn.execute(
            f"INSERT INTO pending_forms ({old_form_cols}) VALUES (?,?,?,?,?,?,?)",
            ("t2", "assistant", "ch1", "root1", "thread", "{}", "2026-01-01"),
        )
        conn.commit()
    finally:
        conn.close()


async def test_a_busy_timeout_is_set_so_the_second_process_waits(tmp_path: Path) -> None:
    # The CLI runs in its own container against the same file; without a timeout
    # a write that lands during the engine's write fails instead of waiting.
    store = SqliteSessionStore(tmp_path / "db.sqlite")
    try:
        timeout = store._conn.execute("PRAGMA busy_timeout").fetchone()[0]
        assert timeout == 5000
    finally:
        await store.close()


async def test_a_lost_claim_leaves_no_transaction_open(tmp_path: Path) -> None:
    # A no-match UPDATE still opens a transaction; without a rollback the write
    # lock would be held until something else committed. The conformance suite
    # pins that the loser writes nothing; this pins HOW sqlite3 is left.
    store = SqliteSessionStore(tmp_path / "db.sqlite")
    try:
        await store.create_task(_task())

        lost = await store.claim_due(
            task_id="tsk_1", seen_due_at="2026-01-01T00:00:00+00:00",  # stale token
            next_run_at=T1, due_at=T1, run=_run(), owner="sched-a",
            lease_until=T2, now=T0,
        )

        assert lost is None
        assert not store._conn.in_transaction
    finally:
        await store.close()


async def test_the_heartbeat_really_is_one_row(tmp_path: Path) -> None:
    # The port only promises that reading gives the latest beat. In SQLite that
    # is a single overwritten row, and an appending bug would still read right
    # while the table grew for as long as the engine ran.
    store = SqliteSessionStore(tmp_path / "db.sqlite")
    try:
        beat = SchedulerHeartbeat(
            scheduler_id="sched-a", pid=42, version="0.7.1", started_at=T0,
            last_tick_at=T0, tick_seq=1, interval_s=20.0, next_wake_at=T1,
            next_task_id="tsk_1", next_task_name="digest", running_count=0,
            tasks_total=1, last_error="", last_error_at="",
        )
        await store.write_heartbeat(beat)
        await store.write_heartbeat(
            SchedulerHeartbeat(**{**beat.__dict__, "tick_seq": 2, "last_tick_at": T1})
        )

        rows = store._conn.execute("SELECT count(*) FROM scheduler_heartbeat")
        assert rows.fetchone()[0] == 1
    finally:
        await store.close()


async def test_no_table_can_hold_a_secret_value(tmp_path: Path) -> None:
    """The whole point of the split: whatever a caller was handed, the DB file
    must not contain it."""
    store = SqliteSessionStore(tmp_path / "db.sqlite")
    try:
        await store.create_grant(_grant())
        await store.record_decision(_audit())
        _assert_no_secret_columns(store)
    finally:
        await store.close()


def _assert_no_secret_columns(store: SqliteSessionStore) -> None:
    with store._lock:  # reaching past the port is the point: read the raw file
        tables = [
            row[0]
            for row in store._conn.execute(
                "SELECT name FROM sqlite_master WHERE name LIKE 'approval_%'"
            )
        ]
        assert set(tables) == {"approval_grants", "approval_audit"}
        columns: set[str] = set()
        for table in tables:
            columns |= {
                row[1] for row in store._conn.execute(f"PRAGMA table_info({table})")
            }
    assert not columns & {"value", "secret_value", "ciphertext", "token"}
