"""What the engine keeps on disk is its owner's alone, whatever the umask."""

import asyncio
import os
import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from crucible.attachments import AttachmentStore
from crucible.config import Settings
from crucible.ports.chat.types import KIND_DM
from crucible.private_files import private_dir, private_file, write_private
from crucible.runtimes.pi.hosts.local import LocalHost
from crucible.runtimes.pi.spawn import SpawnRequest
from crucible.sessions_cli import purge_idle
from crucible.store.sqlite import SqliteSessionStore
from crucible.tools.registry import ToolRegistry


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


@pytest.fixture(autouse=True)
def _permissive_umask():
    """The case that matters: a process whose umask would leave things readable."""
    previous = os.umask(0o022)
    yield
    os.umask(previous)


def test_a_private_directory_is_narrowed_even_when_it_already_existed(tmp_path: Path) -> None:
    wide = tmp_path / "a" / "b"
    wide.mkdir(parents=True)
    wide.chmod(0o755)
    assert _mode(private_dir(wide)) == 0o700
    assert _mode(tmp_path / "a") == 0o755  # the parent keeps what it had


def test_a_private_file_keeps_its_content_and_loses_its_readers(tmp_path: Path) -> None:
    path = tmp_path / "f"
    path.write_text("kept")
    path.chmod(0o644)
    assert _mode(private_file(path)) == 0o600 and path.read_text() == "kept"
    assert _mode(write_private(tmp_path / "new", "x")) == 0o600


def test_conversation_memory_is_the_owners_alone(tmp_path: Path) -> None:
    request = SpawnRequest(
        agent="assistant", profile_dir=tmp_path, session_dir=tmp_path / "s", session_id="assistant--c1"
    )
    resolved = LocalHost._session_dir(request)
    assert resolved is not None and _mode(resolved) == 0o700 and _mode(resolved.parent) == 0o700


async def test_the_inventory_and_its_write_ahead_log_are_the_owners_alone(tmp_path: Path) -> None:
    store = SqliteSessionStore(tmp_path / "db.sqlite")
    try:
        await store.get_or_create("assistant", "dm1", "dm1", KIND_DM)
        assert _mode(tmp_path / "db.sqlite") == 0o600
        for sibling in tmp_path.glob("db.sqlite-*"):
            assert _mode(sibling) == 0o600, sibling.name
    finally:
        await store.close()


def test_a_tool_manifest_is_the_owners_alone(tmp_path: Path) -> None:
    path = ToolRegistry(()).write_manifest(tmp_path / "manifests", "assistant", ())
    assert _mode(path) == 0o600 and _mode(path.parent) == 0o700


async def test_what_people_sent_is_the_owners_alone(tmp_path: Path) -> None:
    store = AttachmentStore(tmp_path / "files", max_bytes=1024, retention_days=0)
    saved = await store.save("assistant", "dm1", "note.txt", b"hello", key="k1")
    assert _mode(Path(saved.path)) == 0o600
    assert _mode(Path(saved.path).parent) == 0o700


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

        forgotten = await purge_idle(settings, store, days=30)
        assert [r.conversation_id for r in forgotten] == ["old"]
        assert not old_file.parent.exists() and fresh_file.exists()
        assert [r.conversation_id for r in await store.list("assistant")] == ["fresh"]
        assert await purge_idle(settings, store, days=30) == []
    finally:
        await store.close()


def test_a_planted_link_is_not_written_through(tmp_path: Path) -> None:
    """A volume an agent can write may hold a link where the engine is about to
    put files; the engine refuses rather than act on whatever it points at."""
    target = tmp_path / "elsewhere"
    target.mkdir()
    target.chmod(0o755)
    (tmp_path / "conv").symlink_to(target)
    with pytest.raises(OSError, match="symbolic link"):
        private_dir(tmp_path / "conv")
    assert _mode(target) == 0o755
    (tmp_path / "f").symlink_to(target / "victim")
    with pytest.raises(OSError):
        private_file(tmp_path / "f")
    assert not (target / "victim").exists()


async def test_a_record_whose_time_does_not_parse_is_kept_and_does_not_stop_the_sweep(
    tmp_path: Path,
) -> None:
    from crucible.sessions_cli import _idle_before
    from crucible.store.base import SessionRecord

    cutoff = datetime.now(timezone.utc)
    def rec(last: str) -> SessionRecord:
        return SessionRecord(agent="a", channel_id="c", conversation_id="x", kind="dm",
                             runtime_session_id="a--x", created_at=last, last_active=last)
    assert _idle_before(rec("not a time"), cutoff) is False
    assert _idle_before(rec("2020-01-01T00:00:00"), cutoff) is True  # naive: read as UTC
    assert _idle_before(rec("2999-01-01T00:00:00+00:00"), cutoff) is False
