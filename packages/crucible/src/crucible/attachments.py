"""AttachmentStore: where files that arrive with a message land on local disk.

A plain file store, like the skill library — no engine machinery, no database.
The gateway downloads an attachment while normalizing the event and saves it
here; from then on everything upstream (the flow, the runtime, the agent's own
``read``/``bash``) deals with an ordinary local path.

Layout::

    <root>/<agent>/<conversation>/<key>-<name>
    <root>/.received/<agent>/<conversation>.jsonl   (what arrived, with digests)

The conversation subdirectory keeps one thread's files together (an agent asked
"the file I sent earlier" can list them), and ``key`` — the platform's own file
id when there is one — makes a save idempotent: a chat platform that redelivers
a message writes the same path instead of a second copy.

Outside every agent's tree — no confinement level grants ``<root>/.received``
to an agent's process, whatever directory it may write — the store keeps what
arrived (``ports.attachments``): a line per file with its size and sha256, so a
tool that hands a person's file on can tell it from one the agent made or
changed.

A conversation's files live as long as the conversation does: ``forget`` takes
the directory and its record together, and who calls it when — the retention
sweep (``attachment_retention``), a conversation forgotten by age or by hand
(``session_retention``, ``sessions_cli``) — is not the store's to know.
"""

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import re
import shutil
import stat
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from crucible.ports.attachments import (
    ReceivedFile,
    ReceivedFileChanged,
    ReceivedFileGone,
    ReceivedFiles,
)
from crucible.ports.chat.types import Attachment
from crucible.private_files import private_dir, private_file

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
# Hex characters of the digest that keeps two conversation ids apart.
_DIGEST_CHARS = 12
_FALLBACK_NAME = "file"

class AttachmentTooLarge(Exception):
    """The file exceeds the configured per-file limit and was not saved."""


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
    def __init__(self, root: str | Path, *, max_bytes: int) -> None:
        self.root = Path(root)
        self.max_bytes = max_bytes

    def dir_for(self, agent: str) -> Path:
        """This agent's own attachment tree — the root it may send files from."""
        return self.root / safe_name(agent)

    def conversation_dir(self, agent: str, conversation_id: str) -> Path:
        """One conversation's attachments, inside its agent's tree, under a name
        no other conversation's maps to. ``safe_name`` alone would not do: it
        drops everything before a ``/`` and folds other characters to ``_``, so
        a caller choosing its own conversation ids (``x/a_b`` against another's
        ``a:b``) could land in someone else's directory — the one a process
        confined to that conversation is given. A digest of the id is appended
        whenever cleaning changed it, as for a runtime session id."""
        raw = conversation_id or "misc"
        cleaned = safe_name(raw)
        if cleaned == raw:
            return self.dir_for(agent) / cleaned
        digest = hashlib.sha256(raw.encode()).hexdigest()[:_DIGEST_CHARS]
        return self.dir_for(agent) / f"{cleaned[: _MAX_NAME - _DIGEST_CHARS - 1]}-{digest}"

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
        target = self.conversation_dir(agent, conversation_id) / filename
        await asyncio.to_thread(self._write, target, data)
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
        arrived: list[ReceivedFile] = []
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
            arrived.append(ReceivedFile(
                path=stored.path, name=stored.name, mime=file.mime, size=stored.size,
                sha256=hashlib.sha256(file.data).hexdigest(),
                received_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            ))
        if arrived:
            await asyncio.to_thread(self._record, self.conversation_dir(agent, conversation_id), arrived)
        return tuple(saved)

    async def adopt(self, agent: str, conversation_id: str, attachment: Attachment) -> Attachment:
        """The attachment under the conversation as the flow keys it. A gateway
        saved it under the key it could compute; when the flow's key differs,
        the file moves into that conversation's directory and its record line
        with it, so a tool's files directory and the record agree with the
        prompt's path. A file outside this store is left where it is."""
        source = Path(attachment.path)
        target_dir = self.conversation_dir(agent, conversation_id)
        if source.parent == target_dir:
            return attachment
        if not source.is_relative_to(self.dir_for(agent)):
            return attachment  # not ours to move (another agent's, or nobody's)
        target = target_dir / source.name
        try:
            await asyncio.to_thread(self._move, source, target)
        except OSError as exc:
            logger.warning("attachment %s could not be moved into its conversation: %s", attachment.name, exc)
            return attachment
        await asyncio.to_thread(self._move_record, source.parent, target_dir, str(source), str(target))
        return Attachment(name=attachment.name, path=str(target), mime=attachment.mime, size=attachment.size)

    @staticmethod
    def _move(source: Path, target: Path) -> None:
        private_dir(target.parent)
        if target.exists() and target.stat().st_size == source.stat().st_size:
            source.unlink()  # a redelivery already adopted
            return
        os.replace(source, target)

    def _move_record(self, from_dir: Path, to_dir: Path, old_path: str, new_path: str) -> None:
        source_record = self._record_path(from_dir)
        records = self._read_record(source_record)
        moving = [r for r in records if r.path == old_path]
        staying = [r for r in records if r.path != old_path]
        if moving:
            if staying:
                private_file(source_record)
                source_record.write_text("".join(json.dumps(r.__dict__, ensure_ascii=False) + "\n" for r in staying), encoding="utf-8")
            else:
                source_record.unlink(missing_ok=True)
            self._record(to_dir, [ReceivedFile(**{**m.__dict__, "path": new_path}) for m in moving])
        with contextlib.suppress(OSError):
            from_dir.rmdir()  # the gateway's directory, when the move emptied it

    # -- a conversation's lifetime -----------------------------------------------

    async def forget(self, agent: str, conversation_id: str) -> int:
        """Remove everything the conversation has here — its directory, files
        the agent made in it included, and its record of what arrived — and
        say how many files went. Nothing of it is kept for a conversation the
        engine has forgotten or that nobody has touched in a while."""
        return await asyncio.to_thread(self._forget_dir, self.conversation_dir(agent, conversation_id))

    def conversation_dirs(self) -> list[Path]:
        """Every conversation directory the store holds, as ``conversation_dir``
        spells them — for a sweep to compare with what the engine still knows."""
        if not self.root.is_dir():
            return []
        found: list[Path] = []
        for agent_dir in sorted(self.root.iterdir()):
            if not agent_dir.is_dir() or agent_dir.name.startswith("."):
                continue  # ``.received`` is the store's own, not a conversation's
            found.extend(child for child in sorted(agent_dir.iterdir()) if child.is_dir())
        return found

    def forget_dir(self, conversation_dir: Path) -> int:
        """``forget`` for a directory ``conversation_dirs`` listed — one the
        engine has no conversation for any more."""
        return self._forget_dir(conversation_dir)

    @staticmethod
    def newest_mtime(conversation_dir: Path) -> float:
        """When the conversation's directory was last written to: the newest of
        its files, or the directory's own time when it holds none."""
        times = [conversation_dir.stat().st_mtime]
        for path in conversation_dir.rglob("*"):
            with contextlib.suppress(OSError):
                times.append(path.stat().st_mtime)
        return max(times)

    def _forget_dir(self, conversation_dir: Path) -> int:
        if not conversation_dir.is_relative_to(self.root) or conversation_dir == self.root:
            raise ValueError(f"{conversation_dir} is not a conversation directory of this store")
        removed = sum(1 for p in conversation_dir.rglob("*") if p.is_file()) if conversation_dir.is_dir() else 0
        shutil.rmtree(conversation_dir, ignore_errors=True)
        record = self._record_path(conversation_dir)
        record.unlink(missing_ok=True)
        for parent in (conversation_dir.parent, record.parent):
            with contextlib.suppress(OSError):
                parent.rmdir()  # the agent's directory, when this emptied it
        return removed

    # -- what arrived --------------------------------------------------------------

    def received_files_for(self, files_dir: Path) -> ReceivedFiles:
        """The files the conversation with this directory received, as the
        port a tool is handed."""
        return _Received(self, files_dir)

    def _record_path(self, files_dir: Path) -> Path:
        # Outside every agent's tree: ``dir_for`` never covers ``.received``
        # (an agent's name cannot start with a dot after ``safe_name``), so no
        # confinement level hands an agent's process this file.
        return self.root / ".received" / files_dir.parent.name / f"{files_dir.name}.jsonl"

    def _record(self, files_dir: Path, arrived: list[ReceivedFile]) -> None:
        path = self._record_path(files_dir)
        private_dir(path.parent)
        known = {r.path for r in self._read_record(path)}
        lines = [json.dumps(r.__dict__, ensure_ascii=False) for r in arrived if r.path not in known]
        if not lines:
            return  # a redelivery: the same files again
        private_file(path)
        with path.open("a", encoding="utf-8") as handle:
            handle.write("".join(line + "\n" for line in lines))

    @staticmethod
    def _read_record(path: Path) -> list[ReceivedFile]:
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            return []
        records: list[ReceivedFile] = []
        for line in text.splitlines():
            try:
                data = json.loads(line)
                records.append(ReceivedFile(
                    path=str(data["path"]), name=str(data["name"]), mime=str(data.get("mime", "")),
                    size=int(data["size"]), sha256=str(data["sha256"]), received_at=str(data.get("received_at", "")),
                ))
            except (ValueError, KeyError, TypeError):
                continue  # a line nobody wrote whole
        return records

    @staticmethod
    def _read_checked(file: ReceivedFile) -> bytes:
        """The bytes at the recorded path, when they are still the recorded
        file: a regular file (no symlink, no pipe), the recorded size and
        digest — all judged on the one descriptor the bytes are read from, so
        nothing swapped in between a check and the read is read."""
        try:
            fd = os.open(file.path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
        except FileNotFoundError as exc:
            raise ReceivedFileGone(file.name) from exc
        except OSError as exc:  # a symlink (ELOOP), a directory, a device
            raise ReceivedFileChanged(file.name) from exc
        try:
            status = os.fstat(fd)
            if not stat.S_ISREG(status.st_mode) or status.st_size != file.size:
                raise ReceivedFileChanged(file.name)
            chunks: list[bytes] = []
            wanted = file.size + 1
            while wanted > 0:
                chunk = os.read(fd, wanted)
                if not chunk:
                    break
                chunks.append(chunk)
                wanted -= len(chunk)
        finally:
            os.close(fd)
        data = b"".join(chunks)
        if len(data) != file.size or hashlib.sha256(data).hexdigest() != file.sha256:
            raise ReceivedFileChanged(file.name)
        return data

    # -- internals ----------------------------------------------------------

    @staticmethod
    def _write(target: Path, data: bytes) -> None:
        # What people sent is the conversation's: the directory and the file
        # are the owner's alone.
        private_dir(target.parent)
        # A keyed re-save of the same bytes is a redelivery, not new content.
        if target.exists() and target.stat().st_size == len(data):
            return
        private_file(target)
        target.write_bytes(data)

    def _prune_empty(self, directory: Path) -> None:
        for child in sorted(directory.iterdir(), reverse=True):
            if child.is_dir():
                self._prune_empty(child)
                try:
                    child.rmdir()
                except OSError:  # not empty — that's the normal case
                    pass


class _Received:
    """``ReceivedFiles`` for one conversation's directory."""

    def __init__(self, store: AttachmentStore, files_dir: Path) -> None:
        self._store = store
        self._files_dir = files_dir

    async def list(self) -> tuple[ReceivedFile, ...]:
        return tuple(await asyncio.to_thread(self._store._read_record, self._store._record_path(self._files_dir)))

    async def read(self, file: ReceivedFile) -> bytes:
        if file.path not in {r.path for r in await self.list()}:
            raise ReceivedFileChanged(file.name)  # not a file this conversation received
        return await asyncio.to_thread(self._store._read_checked, file)
