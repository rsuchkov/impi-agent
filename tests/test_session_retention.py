"""Forgetting conversations by age, and the runtime's storage at startup."""

import asyncio
import logging
import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from crucible.config import Settings
from crucible.ports.chat.types import KIND_DM
from crucible.runtime_storage import (
    RUNTIME_AGENT_DIR_FILES,
    prepare_runtime_agent_dir,
    prepare_runtime_storage,
)
from crucible.session_retention import idle_before, purge_idle
from crucible.store.base import SessionRecord
from crucible.store.sqlite import SqliteSessionStore
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


async def test_the_engine_moves_old_memory_then_forgets_idle_conversations_at_startup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(tmp_path / "pi-agent"))
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

        await prepare_runtime_storage(settings, store)
        assert not flat.exists() and not (agent_dir / old.runtime_session_id).exists()  # moved, then forgotten
        assert (agent_dir / "assistant--other" / kept.name).is_file()  # moved, kept
        assert await store.list("assistant") == []
        # ...and the runtime's agent directory holds what a confined process reads.
        assert all((tmp_path / "pi-agent" / name).is_file() for name in RUNTIME_AGENT_DIR_FILES)
    finally:
        await store.close()


def test_the_agent_directory_gets_empty_private_stores_and_keeps_existing_ones(tmp_path: Path) -> None:
    # A confined runtime process may only read these files; it cannot create one.
    agent_dir = tmp_path / "pi" / "agent"
    agent_dir.mkdir(parents=True)
    (agent_dir / "auth.json").write_text('{"p": {"type": "api_key"}}')

    assert prepare_runtime_agent_dir(agent_dir)
    assert (agent_dir / "auth.json").read_text() == '{"p": {"type": "api_key"}}'  # untouched
    store = agent_dir / "models-store.json"
    # Empty: the runtime reads it as nothing yet, the installer as no login.
    assert store.read_bytes() == b""
    assert stat.S_IMODE(store.stat().st_mode) == 0o600
    assert prepare_runtime_agent_dir(agent_dir)  # again: nothing to do


def test_an_agent_directory_the_runtime_cannot_lock_in_is_an_error(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    agent_dir = tmp_path / "agent"
    agent_dir.mkdir()
    for name in RUNTIME_AGENT_DIR_FILES:
        (agent_dir / name).touch()
    agent_dir.chmod(0o500)  # a read-only image's directory
    try:
        with caplog.at_level(logging.ERROR, logger="crucible.runtime_storage"):
            assert not prepare_runtime_agent_dir(agent_dir)
    finally:
        agent_dir.chmod(0o700)
    assert "point PI_CODING_AGENT_DIR at a writable directory" in caplog.text
    assert str(agent_dir) in caplog.text


async def test_a_forgotten_conversation_loses_its_own_tool_manifest_too(tmp_path: Path) -> None:
    from crucible.tool_manifests import forget_session_manifest, session_manifest_path

    store = SqliteSessionStore(tmp_path / "db.sqlite")
    try:
        old, _ = await store.get_or_create("assistant", "old", "old", KIND_DM)
        long_ago = (datetime.now(timezone.utc) - timedelta(days=40)).isoformat(timespec="seconds")
        await asyncio.to_thread(
            store._conn.execute,
            "UPDATE sessions SET last_active = ? WHERE conversation_id = ?", (long_ago, "old"),
        )
        manifests = tmp_path / "tool-manifests"
        manifest = session_manifest_path(manifests, "assistant", old.runtime_session_id)
        manifest.parent.mkdir(parents=True)
        manifest.write_text("[]")
        agents_own = manifests / "assistant.json"
        agents_own.write_text("[]")

        await purge_idle(tmp_path / "sessions", store, days=30, manifests_dir=manifests)

        assert not manifest.exists() and agents_own.exists()
        # Forgetting what is not there is not an error, nor is a bad id.
        assert forget_session_manifest(manifests, "assistant", old.runtime_session_id) is False
        assert forget_session_manifest(manifests, "assistant", "../assistant") is False
    finally:
        await store.close()


def test_the_applications_model_definitions_reach_the_agent_directory_once(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    from crucible.runtime_storage import provide_model_definitions

    agent_dir = tmp_path / "agent"
    agent_dir.mkdir()
    source = tmp_path / "image" / "models.json"
    source.parent.mkdir()
    source.write_text('{"providers": {}}')

    assert provide_model_definitions(agent_dir, source) is True
    copied = agent_dir / "models.json"
    assert copied.read_text() == '{"providers": {}}'
    assert stat.S_IMODE(copied.stat().st_mode) == 0o600
    # An operator's edit is not overwritten by the image's copy.
    copied.write_text('{"providers": {"mine": {}}}')
    assert provide_model_definitions(agent_dir, source) is False
    assert copied.read_text() == '{"providers": {"mine": {}}}'
    # A source that is not there is said, not raised.
    with caplog.at_level(logging.WARNING):
        assert provide_model_definitions(tmp_path / "other", tmp_path / "nowhere.json") is False
    assert "no model definitions" in caplog.text


async def test_prepare_runtime_storage_copies_the_model_definitions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(tmp_path / "pi-agent"))
    settings = ImpiSettings(
        _env_file=None,  # pyright: ignore[reportCallIssue]
        data_dir=str(tmp_path / "data"), session_retention_days=0,
    )
    source = tmp_path / "models.json"
    source.write_text("{}")
    store = SqliteSessionStore(tmp_path / "db.sqlite")
    try:
        await prepare_runtime_storage(settings, store, models_from=source)
        assert (tmp_path / "pi-agent" / "models.json").read_text() == "{}"
    finally:
        await store.close()


async def test_deleting_a_conversation_by_hand_takes_its_manifest_too(tmp_path: Path, capsys) -> None:
    import argparse

    from crucible.sessions_cli import cmd_delete
    from crucible.tool_manifests import session_manifest_path

    settings = Settings(
        _env_file=None,  # pyright: ignore[reportCallIssue]
        data_dir=str(tmp_path / "data"),
    )
    store = SqliteSessionStore(tmp_path / "db.sqlite")
    try:
        record, _ = await store.get_or_create("assistant", "dm1", "dm1", KIND_DM)
        manifest = session_manifest_path(tmp_path / "data" / "tool-manifests", "assistant", record.runtime_session_id)
        manifest.parent.mkdir(parents=True)
        manifest.write_text("[]")

        await cmd_delete(settings, store, argparse.Namespace(agent="assistant", conversation_id="dm1"))

        assert not manifest.exists()
        assert "deleted assistant/dm1" in capsys.readouterr().out
    finally:
        await store.close()
