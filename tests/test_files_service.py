"""ChatFileService: resolve the conversation, police the path, post the file."""

from pathlib import Path

import pytest

from crucible.interactions.files import ChatFileService, default_roots
from crucible.ports.chat.files import FileError
from crucible.ports.chat.types import KIND_DM, KIND_THREAD
from crucible.store.sessions import SqliteSessionStore
from tests.fakes.fake_chat import FakeChat
from tests.fakes.presence import presence_of

MB = 1024 * 1024


def _service(
    store: SqliteSessionStore, chat: FakeChat, roots: tuple[Path, ...], *, max_bytes: int = MB
) -> ChatFileService:
    return ChatFileService(
        presence_of(chat), store, {"assistant": roots}, max_bytes=max_bytes
    )


async def test_a_file_lands_in_the_thread_the_turn_runs_in(tmp_path: Path) -> None:
    store = SqliteSessionStore(tmp_path / "db.sqlite")
    chat = FakeChat()
    try:
        record, _ = await store.get_or_create("assistant", "ch1", "root1", KIND_THREAD)
        chart = tmp_path / "chart.png"
        chart.write_bytes(b"PNGDATA")
        svc = _service(store, chat, (tmp_path,))

        sent = await svc.send(
            "assistant", record.runtime_session_id, [str(chart)], text="here it is"
        )

        assert sent == ["chart.png"]
        (ref, files, text) = chat.posted_files[0]
        assert ref.channel_id == "ch1" and ref.thread_root_id == "root1"
        assert text == "here it is"
        assert (files[0].name, files[0].data, files[0].mime) == (
            "chart.png", b"PNGDATA", "image/png",
        )
    finally:
        await store.close()


async def test_a_dm_gets_the_file_top_level(tmp_path: Path) -> None:
    store = SqliteSessionStore(tmp_path / "db.sqlite")
    chat = FakeChat()
    try:
        record, _ = await store.get_or_create("assistant", "dm1", "dm1", KIND_DM)
        note = tmp_path / "note.txt"
        note.write_text("hello")

        await _service(store, chat, (tmp_path,)).send(
            "assistant", record.runtime_session_id, [str(note)]
        )

        (ref, _, _) = chat.posted_files[0]
        assert ref.thread_root_id == ""
    finally:
        await store.close()


async def test_a_path_outside_the_agents_roots_is_refused(tmp_path: Path) -> None:
    store = SqliteSessionStore(tmp_path / "db.sqlite")
    chat = FakeChat()
    try:
        record, _ = await store.get_or_create("assistant", "dm1", "dm1", KIND_DM)
        allowed = tmp_path / "workspace"
        allowed.mkdir()
        secret = tmp_path / "secret.env"
        secret.write_text("TOKEN=x")

        with pytest.raises(FileError, match="outside"):
            await _service(store, chat, (allowed,)).send(
                "assistant", record.runtime_session_id, [str(secret)]
            )

        assert chat.posted_files == []
    finally:
        await store.close()


async def test_a_symlink_cannot_lead_out_of_the_roots(tmp_path: Path) -> None:
    store = SqliteSessionStore(tmp_path / "db.sqlite")
    chat = FakeChat()
    try:
        record, _ = await store.get_or_create("assistant", "dm1", "dm1", KIND_DM)
        allowed = tmp_path / "workspace"
        allowed.mkdir()
        secret = tmp_path / "secret.env"
        secret.write_text("TOKEN=x")
        (allowed / "innocent.txt").symlink_to(secret)

        with pytest.raises(FileError, match="outside"):
            await _service(store, chat, (allowed,)).send(
                "assistant", record.runtime_session_id, [str(allowed / "innocent.txt")]
            )
    finally:
        await store.close()


async def test_a_missing_file_and_an_oversized_one_say_why(tmp_path: Path) -> None:
    store = SqliteSessionStore(tmp_path / "db.sqlite")
    chat = FakeChat()
    try:
        record, _ = await store.get_or_create("assistant", "dm1", "dm1", KIND_DM)
        big = tmp_path / "big.bin"
        big.write_bytes(b"x" * 20)
        svc = _service(store, chat, (tmp_path,), max_bytes=8)

        with pytest.raises(FileError, match="no such file"):
            await svc.send("assistant", record.runtime_session_id, [str(tmp_path / "nope")])
        with pytest.raises(FileError, match="over the"):
            await svc.send("assistant", record.runtime_session_id, [str(big)])
    finally:
        await store.close()


async def test_an_unresolvable_conversation_is_reported_not_swallowed(tmp_path: Path) -> None:
    store = SqliteSessionStore(tmp_path / "db.sqlite")
    chat = FakeChat()
    try:
        note = tmp_path / "note.txt"
        note.write_text("hi")

        with pytest.raises(FileError, match="conversation"):
            await _service(store, chat, (tmp_path,)).send(
                "assistant", "assistant--never-seen", [str(note)]
            )
    finally:
        await store.close()


async def test_more_files_than_one_message_can_carry_is_refused(tmp_path: Path) -> None:
    store = SqliteSessionStore(tmp_path / "db.sqlite")
    chat = FakeChat()
    try:
        record, _ = await store.get_or_create("assistant", "dm1", "dm1", KIND_DM)
        paths = []
        for index in range(6):
            path = tmp_path / f"f{index}.txt"
            path.write_text("x")
            paths.append(str(path))

        with pytest.raises(FileError, match="at most"):
            await _service(store, chat, (tmp_path,)).send(
                "assistant", record.runtime_session_id, paths
            )
    finally:
        await store.close()


def test_default_roots_are_the_profile_its_attachments_and_tmp(tmp_path: Path) -> None:
    profile = tmp_path / "agents" / "assistant"
    profile.mkdir(parents=True)
    attachments = tmp_path / "data" / "attachments" / "assistant"
    attachments.mkdir(parents=True)

    roots = default_roots(profile, attachments)

    assert profile.resolve() in roots
    assert attachments.resolve() in roots
    assert len(roots) == 3  # ...plus the system temp directory
