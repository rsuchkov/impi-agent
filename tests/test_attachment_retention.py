"""A conversation's files live as long as the conversation: forgotten with it,
or when it has been idle for the retention."""

import asyncio
import contextlib
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from crucible.attachment_retention import (
    SweptAttachments,
    run_attachment_sweeps,
    sweep_attachments,
)
from crucible.attachments import AttachmentStore, IncomingFile
from crucible.ports.chat.types import KIND_DM
from crucible.store.sqlite import SqliteSessionStore


async def _age(store: SqliteSessionStore, conversation_id: str, days: int) -> None:
    then = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
    await asyncio.to_thread(
        store._conn.execute, "UPDATE sessions SET last_active = ? WHERE conversation_id = ?", (then, conversation_id),
    )


async def test_files_go_with_an_idle_conversation_an_old_orphan_and_nothing_else(tmp_path: Path) -> None:
    """Idleness is the conversation's, not the file's: an old file in a live
    conversation stays, a fresh file in an idle one goes with its record. A
    directory the engine has no conversation for goes once nothing has written
    to it for the retention."""
    files = AttachmentStore(tmp_path / "files", max_bytes=1024)
    sessions = SqliteSessionStore(tmp_path / "db.sqlite")
    try:
        await sessions.get_or_create("assistant", "live", "live", KIND_DM)
        await sessions.get_or_create("assistant", "idle", "idle", KIND_DM)
        await _age(sessions, "idle", 20)
        (old_in_live,) = await files.save_many("assistant", "live", [IncomingFile("a.pdf", b"a", "application/pdf", key="f1")])
        aged = time.time() - 60 * 86400
        os.utime(old_in_live.path, (aged, aged))
        (fresh_in_idle,) = await files.save_many("assistant", "idle", [IncomingFile("b.pdf", b"b", "application/pdf", key="f2")])
        (old_orphan,) = await files.save_many("assistant", "nobody-old", [IncomingFile("c.pdf", b"c", "application/pdf", key="f3")])
        os.utime(old_orphan.path, (aged, aged))
        os.utime(Path(old_orphan.path).parent, (aged, aged))
        (fresh_orphan,) = await files.save_many("assistant", "nobody-new", [IncomingFile("d.pdf", b"d", "application/pdf", key="f4")])
        received = tmp_path / "files" / ".received" / "assistant"

        assert await sweep_attachments(files, sessions, days=14) == SweptAttachments(idle_conversations=1, without_conversation=1, files=2)
        assert Path(old_in_live.path).is_file() and (received / "live.jsonl").is_file()
        assert not Path(fresh_in_idle.path).parent.exists() and not (received / "idle.jsonl").exists()
        assert not Path(old_orphan.path).parent.exists() and not (received / "nobody-old.jsonl").exists()
        assert Path(fresh_orphan.path).is_file()
        assert [r.conversation_id for r in await sessions.list("assistant")] == ["live", "idle"]  # records are not this sweep's
        assert await sweep_attachments(files, sessions, days=14) == SweptAttachments()
        assert await sweep_attachments(files, sessions, days=0) == SweptAttachments()  # 0: keep everything
    finally:
        await sessions.close()


async def test_the_sweep_runs_now_and_every_period_and_a_retention_of_zero_does_not_run(tmp_path: Path) -> None:
    files = AttachmentStore(tmp_path / "files", max_bytes=1024)
    sessions = SqliteSessionStore(tmp_path / "db.sqlite")
    slept: list[float] = []

    async def sleep(seconds: float) -> None:
        slept.append(seconds)
        if len(slept) >= 2:
            raise RuntimeError("stop")

    try:
        await sessions.get_or_create("assistant", "idle", "idle", KIND_DM)
        await _age(sessions, "idle", 20)
        (saved,) = await files.save_many("assistant", "idle", [IncomingFile("b.pdf", b"b", "application/pdf", key="f2")])
        try:
            await run_attachment_sweeps(files, sessions, days=14, interval_s=5.0, sleep=sleep)
        except RuntimeError:
            pass
        assert not Path(saved.path).exists() and slept == [5.0, 5.0]  # swept before the first sleep
        await run_attachment_sweeps(files, sessions, days=0, sleep=sleep)
        assert slept == [5.0, 5.0]  # returned at once
        # A pass that fails is logged and the loop goes on to the next period.
        await sessions.close()
        slept.clear()
        with pytest.raises(RuntimeError, match="stop"):
            await run_attachment_sweeps(files, sessions, days=14, interval_s=7.0, sleep=sleep)  # the store is closed: list() raises
        assert slept == [7.0, 7.0]
    finally:
        with contextlib.suppress(Exception):
            await sessions.close()
