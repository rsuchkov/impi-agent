"""ChatFileService: the concrete FileService — resolve the conversation, read the
file, post it as the agent.

It shares the resolution the widget service does (session record -> where to
post, presence -> the agent's own client), which is why it lives beside it.

The path policy is here: an agent may send from its own profile directory (its
working directory), its own attachment directory (whatever people sent it), and
the system temp directory (where a turn's scratch output naturally lands). A path
is resolved before the check, so a symlink cannot lead out of those roots.
"""

import asyncio
import logging
import mimetypes
import tempfile
from collections.abc import Mapping
from pathlib import Path

from crucible.interactions.presence import AgentPresence
from crucible.interactions.service import conversation_ref
from crucible.ports.chat.files import FileError
from crucible.ports.chat.types import OutgoingFile
from crucible.store.base import SessionStore

logger = logging.getLogger(__name__)

# How many files one call may send: enough for "here are the three charts",
# small enough that a confused agent can't flood a conversation.
MAX_FILES_PER_SEND = 5


class ChatFileService:
    def __init__(
        self,
        presence: AgentPresence,
        sessions: SessionStore,
        roots: Mapping[str, tuple[Path, ...]],
        *,
        max_bytes: int,
    ) -> None:
        self._presence = presence
        self._sessions = sessions
        self._roots = roots
        self._max_bytes = max_bytes

    async def send(
        self, agent: str, runtime_session_id: str, paths: list[str], *, text: str = ""
    ) -> list[str]:
        if not paths:
            raise FileError("no file to send")
        if len(paths) > MAX_FILES_PER_SEND:
            raise FileError(f"at most {MAX_FILES_PER_SEND} files at a time")

        record = await self._sessions.get_by_runtime_session(runtime_session_id)
        poster = self._presence.poster(agent)
        if record is None or poster is None:
            logger.warning("send file: no session/poster for %s / %s", agent, runtime_session_id)
            raise FileError("this conversation could not be resolved — no file was sent")

        files = [await asyncio.to_thread(self._read, agent, path) for path in paths]
        await poster.post_files(conversation_ref(record), files, text=text)
        logger.info("agent %s sent %d file(s)", agent, len(files))
        return [file.name for file in files]

    # -- internals ----------------------------------------------------------

    def _read(self, agent: str, path: str) -> OutgoingFile:
        resolved = Path(path).expanduser().resolve()
        if not self._permitted(agent, resolved):
            raise FileError(
                f"{path}: outside the directories you may send from "
                f"({', '.join(str(r) for r in self._roots.get(agent, ()))})"
            )
        if not resolved.is_file():
            raise FileError(f"{path}: no such file")
        size = resolved.stat().st_size
        if size > self._max_bytes:
            raise FileError(
                f"{resolved.name}: {size} bytes is over the {self._max_bytes}-byte limit"
            )
        mime, _ = mimetypes.guess_type(resolved.name)
        return OutgoingFile(
            name=resolved.name,
            data=resolved.read_bytes(),
            mime=mime or "application/octet-stream",
        )

    def _permitted(self, agent: str, resolved: Path) -> bool:
        roots = self._roots.get(agent, ())
        return any(resolved.is_relative_to(root) for root in roots)


def default_roots(
    profile_dir: Path, attachments_dir: Path | None, *, include_temp: bool = True
) -> tuple[Path, ...]:
    """The directories an agent may send files from (resolved, so the membership
    test compares like with like).

    ``include_temp`` is False when the agent's runtime does not share this
    process's filesystem. The temp directory is then not one place but two, and
    honouring a path into it would mean sending whatever THIS side happens to
    have there — a different file, possibly another agent's. The agent's own
    directory is shared on purpose and is where it should write instead.
    """
    roots = [profile_dir]
    if include_temp:
        roots.append(Path(tempfile.gettempdir()))
    if attachments_dir is not None:
        roots.append(attachments_dir)
    return tuple(dict.fromkeys(root.expanduser().resolve() for root in roots))
