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
