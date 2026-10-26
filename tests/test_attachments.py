"""AttachmentStore: where files that arrive with a message land on disk."""

import os
import time
from pathlib import Path

import pytest

from crucible.attachments import (
    AttachmentStore,
    AttachmentTooLarge,
    IncomingFile,
    safe_name,
    sniff_image,
)
from crucible.ports.chat.types import Attachment

MB = 1024 * 1024


def _store(tmp_path: Path, *, max_bytes: int = MB, retention_days: int = 14) -> AttachmentStore:
    return AttachmentStore(
        tmp_path / "attachments", max_bytes=max_bytes, retention_days=retention_days
    )


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        ("report.pdf", "report.pdf"),
        ("../../etc/passwd", "passwd"),  # traversal can't survive a basename
        ("C:\\Users\\me\\photo.png", "photo.png"),
        ("", "file"),
        ("...", "file"),
        ("wild name?*.txt", "wild name_.txt"),
    ],
)
def test_safe_name(given: str, expected: str) -> None:
    assert safe_name(given) == expected


def test_long_name_is_capped_but_keeps_its_extension() -> None:
    name = safe_name("x" * 300 + ".png")
    assert len(name) <= 80
    assert name.endswith(".png")


async def test_save_lands_under_agent_and_conversation(tmp_path: Path) -> None:
    store = _store(tmp_path)

    saved = await store.save("assistant", "conv-1", "screen.png", b"bytes", key="f7")

    path = Path(saved.path)
    assert path.read_bytes() == b"bytes"
    assert path.parent == tmp_path / "attachments" / "assistant" / "conv-1"
    assert path.name == "f7-screen.png"
    assert saved.name == "screen.png"
    assert saved.size == 5


async def test_same_key_is_the_same_file_not_a_second_copy(tmp_path: Path) -> None:
    # A platform that redelivers a message must not multiply its attachments.
    store = _store(tmp_path)

    first = await store.save("assistant", "conv-1", "a.png", b"same", key="f7")
    second = await store.save("assistant", "conv-1", "a.png", b"same", key="f7")

    assert first.path == second.path
    assert len(list((tmp_path / "attachments" / "assistant" / "conv-1").iterdir())) == 1


async def test_unkeyed_saves_do_not_collide(tmp_path: Path) -> None:
    store = _store(tmp_path)

    first = await store.save("assistant", "conv-1", "a.png", b"one")
    second = await store.save("assistant", "conv-1", "a.png", b"two")

    assert first.path != second.path


async def test_oversized_file_is_refused(tmp_path: Path) -> None:
    store = _store(tmp_path, max_bytes=8)

    with pytest.raises(AttachmentTooLarge):
        await store.save("assistant", "conv-1", "big.bin", b"x" * 9)


async def test_save_many_keeps_the_mime_and_skips_what_it_cannot_store(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path, max_bytes=8)

    saved = await store.save_many(
        "assistant",
        "conv-1",
        [
            IncomingFile(name="ok.png", data=b"small", mime="image/png", key="f1"),
            IncomingFile(name="big.bin", data=b"x" * 9, mime="application/zip", key="f2"),
        ],
    )

    assert [(a.name, a.mime) for a in saved] == [("ok.png", "image/png")]


async def test_sweep_removes_old_files_and_empty_directories(tmp_path: Path) -> None:
    store = _store(tmp_path, retention_days=7)
    old = await store.save("assistant", "old-conv", "a.png", b"x", key="f1")
    fresh = await store.save("assistant", "new-conv", "b.png", b"y", key="f2")
    aged = time.time() - 8 * 86400
    os.utime(old.path, (aged, aged))

    removed = store.sweep()

    assert removed == 1
    assert not Path(old.path).exists()
    assert Path(fresh.path).exists()
    assert not (tmp_path / "attachments" / "assistant" / "old-conv").exists()


async def test_retention_zero_keeps_everything(tmp_path: Path) -> None:
    store = _store(tmp_path, retention_days=0)
    saved = await store.save("assistant", "conv-1", "a.png", b"x", key="f1")
    aged = time.time() - 900 * 86400
    os.utime(saved.path, (aged, aged))

    assert store.sweep() == 0
    assert Path(saved.path).exists()


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        (b"\x89PNG\r\n\x1a\n" + b"rest", "image/png"),
        (b"\xff\xd8\xff\xe0" + b"rest", "image/jpeg"),
        (b"GIF89a" + b"rest", "image/gif"),
        (b"RIFF\x00\x00\x00\x00WEBPVP8 ", "image/webp"),
        (b"RIFF\x00\x00\x00\x00WAVEfmt ", ""),  # a RIFF container, but audio
        (b"%PDF-1.7", ""),
        (b"", ""),
    ],
)
def test_sniff_image_reads_the_bytes_not_the_label(data: bytes, expected: str) -> None:
    assert sniff_image(data) == expected


def test_two_conversation_ids_never_share_a_directory(tmp_path: Path) -> None:
    """A caller that picks its own ids (a WebSocket service, an http realm) must
    not reach another's directory by spelling an id that cleans to the same
    name: a process confined to its conversation is given that directory."""
    store = _store(tmp_path)
    victim = store.conversation_dir("assistant", "svcB:conv42")
    forged = store.conversation_dir("assistant", "evil:x/svcB_conv42")
    assert victim != forged
    assert store.conversation_dir("assistant", "a:b") != store.conversation_dir("assistant", "a_b")
    # A platform id that is already a safe name stays as it is.
    assert store.conversation_dir("assistant", "q7x3k9m2p4").name == "q7x3k9m2p4"
    assert len(store.conversation_dir("assistant", "w:" + "x" * 300).name) <= 80


async def test_what_arrived_is_recorded_beside_the_conversations_directory_with_its_digest(tmp_path: Path) -> None:
    """A tool that hands a person's file on asks the record, not the directory:
    the directory holds what tools wrote too, and the agent's process may write
    there. The record lives beside it, where that process cannot."""
    import hashlib

    from crucible.ports.attachments import ReceivedFileChanged, ReceivedFileGone

    store = _store(tmp_path)
    saved = await store.save_many("assistant", "conv-1", [
        IncomingFile(name="invoice.pdf", data=b"%PDF-1.4 hello", mime="application/pdf", key="f1"),
        IncomingFile(name="photo.png", data=b"\x89PNG\r\n\x1a\nabc", mime="image/png", key="f2"),
    ])
    files_dir = store.conversation_dir("assistant", "conv-1")
    received = store.received(files_dir)
    listed = await received.list()
    assert [(r.name, r.mime, r.size) for r in listed] == [("invoice.pdf", "application/pdf", 14), ("photo.png", "image/png", 11)]
    assert listed[0].path == saved[0].path and listed[0].sha256 == hashlib.sha256(b"%PDF-1.4 hello").hexdigest()
    assert listed[0].received_at
    record = files_dir.parent / ".conv-1.received.jsonl"
    assert record.is_file()  # beside the directory, not in it: the agent's process cannot write there
    assert not any(child.name.endswith(".received.jsonl") for child in files_dir.iterdir())
    # A redelivery (the same key) is not a second line.
    await store.save_many("assistant", "conv-1", [IncomingFile(name="invoice.pdf", data=b"%PDF-1.4 hello", mime="application/pdf", key="f1")])
    assert len(await received.list()) == 2
    # Reading checks the file is still the one that arrived.
    assert await received.read(listed[0]) == b"%PDF-1.4 hello"
    Path(listed[0].path).write_bytes(b"%PDF-1.4 hellO")  # the agent's process rewrote it (same size)
    with pytest.raises(ReceivedFileChanged):
        await received.read(listed[0])
    Path(listed[1].path).unlink()
    with pytest.raises(ReceivedFileGone):
        await received.read(listed[1])
    # A file in the directory that nobody sent — a tool wrote it — is not a received file.
    stray = files_dir / "made-by-a-tool.txt"
    stray.write_bytes(b"x")
    from crucible.ports.attachments import ReceivedFile

    with pytest.raises(ReceivedFileChanged):
        await received.read(ReceivedFile(str(stray), "made-by-a-tool.txt", "text/plain", 1, hashlib.sha256(b"x").hexdigest(), ""))
    # A symlink standing where the file was: not the file.
    Path(listed[0].path).unlink()
    other = tmp_path / "elsewhere.pdf"
    other.write_bytes(b"%PDF-1.4 hello")
    os.symlink(other, listed[0].path)
    with pytest.raises(ReceivedFileChanged):
        await received.read(listed[0])
    # A conversation that received nothing.
    assert await store.received(store.conversation_dir("assistant", "conv-9")).list() == ()


async def test_a_file_saved_under_one_key_is_adopted_into_the_conversation_as_keyed_with_its_record(tmp_path: Path) -> None:
    store = _store(tmp_path)
    (saved,) = await store.save_many("assistant", "D1", [IncomingFile(name="notes.txt", data=b"abc", mime="text/plain", key="F1")])
    adopted = await store.adopt("assistant", "task-7", saved)
    assert Path(adopted.path).parent == store.conversation_dir("assistant", "task-7")
    assert (adopted.name, adopted.mime, adopted.size) == ("notes.txt", "text/plain", 3) and Path(adopted.path).read_bytes() == b"abc"
    assert not Path(saved.path).exists()
    # The record moved with it: the new conversation received it, the old one no longer lists it.
    assert [r.path for r in await store.received(store.conversation_dir("assistant", "task-7")).list()] == [adopted.path]
    assert await store.received(store.conversation_dir("assistant", "D1")).list() == ()
    assert await store.received(store.conversation_dir("assistant", "task-7")).read((await store.received(store.conversation_dir("assistant", "task-7")).list())[0]) == b"abc"
    # Already there: itself. Not this store's: itself.
    assert await store.adopt("assistant", "task-7", adopted) == adopted
    (tmp_path / "elsewhere.txt").write_bytes(b"not the store's")
    elsewhere = Attachment(name="x", path=str(tmp_path / "elsewhere.txt"), mime="", size=15)
    assert await store.adopt("assistant", "task-7", elsewhere) == elsewhere
    assert (tmp_path / "elsewhere.txt").is_file()  # left where it was, not pulled into a conversation
