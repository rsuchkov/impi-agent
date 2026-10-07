"""Forgetting conversations by age, and the engine's housekeeping at startup."""

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

from crucible.config import Settings
from crucible.ports.chat.types import KIND_DM
from crucible.session_retention import idle_before, purge_idle
from crucible.store.base import SessionRecord
from crucible.store.sqlite import SqliteSessionStore
from impi.app import prepare_conversation_memory
from impi.config import ImpiSettings


async def test_idle_conversations_are_forgotten_record_and_files(tmp_path: Path) -> None:
    """The one place a conversation is forgotten by age: the command and an
    engine that purges at startup both call it."""
    settings = Settings(
        _env_file=None,  # pyright: ignore[reportCallIssue]
        data_dir=str(tmp_path / "data"),
    )
    store = SqliteSessionStore(tmp_path / "db.sqlite")
    try:
        old, _ = await store.get_or_create("assistant", "old", "old", KIND_DM)
        fresh, _ = await store.get_or_create("assistant", "fresh", "fresh", KIND_DM)
        long_ago = (datetime.now(timezone.utc) - timedelta(days=40)).isoformat(timespec="seconds")
        await asyncio.to_thread(
            store._conn.execute,
            "UPDATE sessions SET last_active = ? WHERE conversation_id = ?", (long_ago, "old"),
        )
        sessions = settings.resolved_pi_session_dir / "assistant"
        old_file = sessions / old.runtime_session_id / f"2026-01-01T00-00-00-000Z_{old.runtime_session_id}.jsonl"
        fresh_file = sessions / fresh.runtime_session_id / f"2026-01-01T00-00-00-000Z_{fresh.runtime_session_id}.jsonl"
        for path in (old_file, fresh_file):
            path.parent.mkdir(parents=True)
        old_file.write_text("{}")
        fresh_file.write_text("{}")

        forgotten = await purge_idle(settings.resolved_pi_session_dir, store, days=30)
        assert [r.conversation_id for r in forgotten] == ["old"]
        assert not old_file.parent.exists() and fresh_file.exists()
        assert [r.conversation_id for r in await store.list("assistant")] == ["fresh"]
        assert await purge_idle(settings.resolved_pi_session_dir, store, days=30) == []
    finally:
        await store.close()


async def test_a_record_whose_time_does_not_parse_is_kept_and_does_not_stop_the_sweep(
    tmp_path: Path,
) -> None:
    cutoff = datetime.now(timezone.utc)
    def rec(last: str) -> SessionRecord:
        return SessionRecord(agent="a", channel_id="c", conversation_id="x", kind="dm",
                             runtime_session_id="a--x", created_at=last, last_active=last)
    assert idle_before(rec("not a time"), cutoff) is False
    assert idle_before(rec("2020-01-01T00:00:00"), cutoff) is True  # naive: read as UTC
    assert idle_before(rec("2999-01-01T00:00:00+00:00"), cutoff) is False


async def test_the_engine_moves_old_memory_then_forgets_idle_conversations_at_startup(tmp_path: Path) -> None:
    settings = ImpiSettings(
        _env_file=None,  # pyright: ignore[reportCallIssue]
        data_dir=str(tmp_path / "data"), session_retention_days=30,
    )
    store = SqliteSessionStore(tmp_path / "db.sqlite")
    try:
        old, _ = await store.get_or_create("assistant", "old", "old", KIND_DM)
        long_ago = (datetime.now(timezone.utc) - timedelta(days=40)).isoformat(timespec="seconds")
        await asyncio.to_thread(
            store._conn.execute,
            "UPDATE sessions SET last_active = ? WHERE conversation_id = ?", (long_ago, "old"),
        )
        agent_dir = settings.resolved_pi_session_dir / "assistant"
        agent_dir.mkdir(parents=True)
        flat = agent_dir / f"2026-01-01T00-00-00-000Z_{old.runtime_session_id}.jsonl"
        flat.write_text("{}")  # the layout before a directory per conversation
        kept = agent_dir / "2026-01-01T00-00-00-000Z_assistant--other.jsonl"
        kept.write_text("{}")

        await prepare_conversation_memory(settings, store)
        assert not flat.exists() and not (agent_dir / old.runtime_session_id).exists()  # moved, then forgotten
        assert (agent_dir / "assistant--other" / kept.name).is_file()  # moved, kept
        assert await store.list("assistant") == []
    finally:
        await store.close()
