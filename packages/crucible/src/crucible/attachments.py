"""AttachmentStore: where files that arrive with a message land on local disk.

A plain file store, like the skill library — no engine machinery, no database.
The gateway downloads an attachment while normalizing the event and saves it
here; from then on everything upstream (the flow, the runtime, the agent's own
``read``/``bash``) deals with an ordinary local path.

Layout::

    <root>/<agent>/<conversation>/<key>-<name>

The conversation subdirectory keeps one thread's files together (an agent asked
"the file I sent earlier" can list them), and ``key`` — the platform's own file
id when there is one — makes a save idempotent: a chat platform that redelivers
a message writes the same path instead of a second copy.
"""

import asyncio
import logging
import re
import time
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from crucible.ports.chat.types import Attachment

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class IncomingFile:
    """A file an adapter got off the platform, ready to be stored.

    ``key`` is the platform's own file id when it has one — it makes the save
    idempotent, so a redelivered message writes the same path twice instead of
    two copies."""

    name: str
    data: bytes
    mime: str = ""
    key: str = ""

# Filesystem hygiene for names that come from a stranger's machine.
_UNSAFE = re.compile(r"[^A-Za-z0-9._\- ]+")
_MAX_NAME = 80
_FALLBACK_NAME = "file"
# How often a save may trigger a retention sweep (the sweep also runs at startup).
_SWEEP_INTERVAL_S = 3600.0


# What the image formats a model backend accepts actually start with. Used to
# check bytes against the media type the platform claimed: a runtime session
# replays its history on every later turn, so one picture the backend rejects
# would keep failing the conversation, not merely the turn it arrived in.
_IMAGE_MAGIC: tuple[tuple[bytes, str], ...] = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
)


class AttachmentTooLarge(Exception):
    """The file exceeds the configured per-file limit and was not saved."""


def sniff_image(data: bytes) -> str:
    """The image type these bytes really are, or "" if they are not an image."""
    for magic, mime in _IMAGE_MAGIC:
        if data.startswith(magic):
            return mime
    # WebP is a RIFF container; the format only shows up at offset 8.
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return ""


def safe_name(name: str) -> str:
    """A user-supplied filename reduced to something safe to create.

    Directory components are dropped (only the basename survives), unusual
    characters collapse to ``_``, and the result is length-capped with the
    extension preserved — so ``../../etc/passwd`` can never address anything."""
    base = Path(name.replace("\\", "/")).name.strip()
    cleaned = _UNSAFE.sub("_", base).strip("._ ")
    if not cleaned:
        return _FALLBACK_NAME
    if len(cleaned) <= _MAX_NAME:
        return cleaned
    stem, dot, ext = cleaned.rpartition(".")
    if dot and len(ext) <= 8:
        return stem[: _MAX_NAME - len(ext) - 1] + "." + ext
    return cleaned[:_MAX_NAME]


class AttachmentStore:
    def __init__(
        self,
        root: str | Path,
        *,
        max_bytes: int,
        retention_days: int,
    ) -> None:
        self.root = Path(root)
        self.max_bytes = max_bytes
        self.retention_days = retention_days
        self._last_sweep = 0.0

    def dir_for(self, agent: str) -> Path:
        """This agent's own attachment tree — the root it may send files from."""
        return self.root / safe_name(agent)

    async def save(
        self,
        agent: str,
        conversation_id: str,
        name: str,
        data: bytes,
        *,
        key: str = "",
    ) -> Attachment:
        """Write one file and describe it. Raises AttachmentTooLarge past the cap.

        ``key`` names the file deterministically (the platform's file id); without
        one a timestamp is used, so an unkeyed redelivery would write a second
        copy."""
        if len(data) > self.max_bytes:
            raise AttachmentTooLarge(
                f"{name}: {len(data)} bytes exceeds the {self.max_bytes}-byte limit"
            )
        stamp = key or datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
        filename = f"{safe_name(stamp)}-{safe_name(name)}"
        target = self.dir_for(agent) / safe_name(conversation_id or "misc") / filename
        await asyncio.to_thread(self._write, target, data)
        self._maybe_sweep()
        return Attachment(
            name=Path(name.replace("\\", "/")).name or filename,
            path=str(target),
            size=len(data),
        )

    async def save_many(
        self, agent: str, conversation_id: str, files: Iterable[IncomingFile]
    ) -> tuple[Attachment, ...]:
        """Save what an adapter fetched, keeping the media type it reported.

        A file that can't be saved is logged and skipped: an oversized photo must
        not cost the user their message."""
        saved: list[Attachment] = []
        for file in files:
            try:
                stored = await self.save(
                    agent, conversation_id, file.name, file.data, key=file.key
                )
            except (AttachmentTooLarge, OSError) as exc:
                logger.warning("attachment %s not saved: %s", file.name, exc)
                continue
            saved.append(
                Attachment(
                    name=stored.name,
                    path=stored.path,
                    mime=file.mime,
                    size=stored.size,
                )
            )
        return tuple(saved)

    def sweep(self) -> int:
        """Delete files past the retention window; return how many went. Empty
        directories are pruned so the tree doesn't grow a skeleton of dead
        conversations. ``retention_days <= 0`` keeps everything."""
        self._last_sweep = time.monotonic()
        if self.retention_days <= 0 or not self.root.is_dir():
            return 0
        cutoff = time.time() - self.retention_days * 86400
        removed = 0
        for path in self.root.rglob("*"):
            if not path.is_file():
                continue
            try:
                if path.stat().st_mtime < cutoff:
                    path.unlink()
                    removed += 1
            except OSError as exc:  # a racing reader, a read-only mount
                logger.debug("could not sweep %s: %s", path, exc)
        self._prune_empty(self.root)
        if removed:
            logger.info("attachments: swept %d file(s) older than %d days",
                        removed, self.retention_days)
        return removed

    # -- internals ----------------------------------------------------------

    @staticmethod
    def _write(target: Path, data: bytes) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        # A keyed re-save of the same bytes is a redelivery, not new content.
        if target.exists() and target.stat().st_size == len(data):
            return
        target.write_bytes(data)

    def _maybe_sweep(self) -> None:
        if time.monotonic() - self._last_sweep >= _SWEEP_INTERVAL_S:
            try:
                self.sweep()
            except OSError as exc:
                logger.debug("attachment sweep failed: %s", exc)

    def _prune_empty(self, directory: Path) -> None:
        for child in sorted(directory.iterdir(), reverse=True):
            if child.is_dir():
                self._prune_empty(child)
                try:
                    child.rmdir()
                except OSError:  # not empty — that's the normal case
                    pass
