from pathlib import Path

from crucible.ports.chat.types import KIND_DM, KIND_THREAD
from crucible.runtimes.pi.runtime import _safe_session_id
from crucible.store import SqliteSessionStore, derive_runtime_session_id
from crucible.store.base import FormRecord


async def test_get_or_create_is_idempotent(tmp_path: Path) -> None:
    store = SqliteSessionStore(tmp_path / "db.sqlite")
    try:
        first, created_first = await store.get_or_create("assistant", "ch1", "root1", KIND_THREAD)
        second, created_second = await store.get_or_create("assistant", "ch1", "root1", KIND_THREAD)

        assert created_first is True
        assert created_second is False
        assert first == second
        assert first.runtime_session_id == "assistant--root1"
        assert first.kind == KIND_THREAD
        assert len(await store.list()) == 1
    finally:
        await store.close()


async def test_same_conversation_different_agents_are_distinct(tmp_path: Path) -> None:
    store = SqliteSessionStore(tmp_path / "db.sqlite")
    try:
        a, _ = await store.get_or_create("assistant", "ch1", "root1", KIND_THREAD)
        b, _ = await store.get_or_create("developer", "ch1", "root1", KIND_THREAD)

        assert a.runtime_session_id != b.runtime_session_id
        assert len(await store.list()) == 2
        assert [r.agent for r in await store.list("developer")] == ["developer"]
    finally:
        await store.close()


async def test_records_survive_reopen(tmp_path: Path) -> None:
    db = tmp_path / "db.sqlite"
    store = SqliteSessionStore(db)
    await store.get_or_create("assistant", "dmch", "dmch", KIND_DM)
    await store.close()

    reopened = SqliteSessionStore(db)
    try:
        records = await reopened.list()
        assert len(records) == 1
        assert records[0].conversation_id == "dmch"
        assert records[0].kind == KIND_DM
    finally:
        await reopened.close()


async def test_touch_updates_last_active(tmp_path: Path) -> None:
    store = SqliteSessionStore(tmp_path / "db.sqlite")
    try:
        record, _ = await store.get_or_create("assistant", "ch1", "root1", KIND_THREAD)
        # Force an older timestamp, then touch and compare.
        store._conn.execute(
            "UPDATE sessions SET last_active = '2000-01-01T00:00:00+00:00'"
        )
        store._conn.commit()
        await store.touch("assistant", "root1")

        refreshed = (await store.list())[0]
        assert refreshed.last_active > "2000-01-01"
        assert refreshed.created_at == record.created_at
    finally:
        await store.close()


async def test_delete_returns_record_and_removes_row(tmp_path: Path) -> None:
    store = SqliteSessionStore(tmp_path / "db.sqlite")
    try:
        await store.get_or_create("assistant", "ch1", "root1", KIND_THREAD)

        deleted = await store.delete("assistant", "root1")
        assert deleted is not None and deleted.conversation_id == "root1"
        assert await store.delete("assistant", "root1") is None
        assert await store.list() == []
    finally:
        await store.close()


def test_derived_ids_are_already_safe_for_pi() -> None:
    # CONTRACT: store-derived ids must pass pi's sanitizer unchanged, or the
    # DB inventory and pi's on-disk files drift apart in naming.
    for agent, conv in (
        ("assistant", "8psmi44tz0m1oq5g7fmumtdqcr"),  # MM post id
        ("agent-builder", "wjmu5xdznjruxezxj3mhew4usr"),
        ("dev.agent", "root_id-123"),
    ):
        derived = derive_runtime_session_id(agent, conv)
        assert _safe_session_id(derived) == derived


def test_derive_sanitizes_hostile_input() -> None:
    # Non-ASCII input on purpose: exercises the sanitizer alphabet.
    assert derive_runtime_session_id("агент", "тред/1") == "1"
    assert derive_runtime_session_id("///", "///") == "session"


async def test_get_by_runtime_session_reverse_lookup(tmp_path: Path) -> None:
    store = SqliteSessionStore(tmp_path / "db.sqlite")
    try:
        rec, _ = await store.get_or_create("assistant", "ch1", "root1", KIND_THREAD)
        found = await store.get_by_runtime_session(rec.runtime_session_id)
        assert found is not None
        assert found.channel_id == "ch1" and found.conversation_id == "root1"
        assert await store.get_by_runtime_session("nope") is None
    finally:
        await store.close()


async def test_interaction_is_one_shot(tmp_path: Path) -> None:
    from crucible.store import InteractionRecord

    store = SqliteSessionStore(tmp_path / "db.sqlite")
    try:
        rec = InteractionRecord(
            interaction_id="i1", token="tok", agent="assistant",
            channel_id="ch1", conversation_id="root1", kind=KIND_THREAD, created_at="2026-01-01T00:00:00+00:00",
        )
        await store.create_interaction(rec)
        taken = await store.take_interaction("tok")
        assert taken is not None and taken.conversation_id == "root1"
        # second click with the same token gets nothing (consumed)
        assert await store.take_interaction("tok") is None
        assert await store.take_interaction("unknown") is None
    finally:
        await store.close()


async def test_get_or_create_records_last_user_and_refreshes_it(tmp_path: Path) -> None:
    store = SqliteSessionStore(tmp_path / "db.sqlite")
    try:
        rec, created = await store.get_or_create(
            "assistant", "ch1", "root1", KIND_THREAD, user_id="u-first"
        )
        assert created and rec.last_user_id == "u-first"
        # A later turn by another user in the same conversation refreshes it,
        # so a mid-turn tool addresses THIS turn's user.
        rec2, created2 = await store.get_or_create(
            "assistant", "ch1", "root1", KIND_THREAD, user_id="u-second"
        )
        assert not created2 and rec2.last_user_id == "u-second"
    finally:
        await store.close()


async def test_touch_updates_last_user_when_given(tmp_path: Path) -> None:
    store = SqliteSessionStore(tmp_path / "db.sqlite")
    try:
        await store.get_or_create("assistant", "ch1", "root1", KIND_THREAD, user_id="u1")
        await store.touch("assistant", "root1", user_id="u2")
        rec = await store.get_by_runtime_session(
            derive_runtime_session_id("assistant", "root1")
        )
        assert rec is not None and rec.last_user_id == "u2"
        # touch without a user_id leaves it intact
        await store.touch("assistant", "root1")
        rec2 = await store.get_by_runtime_session(
            derive_runtime_session_id("assistant", "root1")
        )
        assert rec2 is not None and rec2.last_user_id == "u2"
    finally:
        await store.close()


async def test_migration_adds_last_user_id_to_old_db(tmp_path: Path) -> None:
    import sqlite3

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
    import sqlite3

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
    import sqlite3

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


async def test_a_busy_timeout_is_set_so_the_second_process_waits(tmp_path) -> None:
    # The CLI runs in its own container against the same file; without a timeout
    # a write that lands during the engine's write fails instead of waiting.
    store = SqliteSessionStore(tmp_path / "db.sqlite")
    try:
        timeout = store._conn.execute("PRAGMA busy_timeout").fetchone()[0]
        assert timeout == 5000
    finally:
        await store.close()
