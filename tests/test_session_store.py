from pathlib import Path

from crucible.ports.chat.types import KIND_DM, KIND_THREAD
from crucible.runtimes.pi.runtime import _safe_session_id
from crucible.store import SqliteSessionStore, derive_runtime_session_id


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
