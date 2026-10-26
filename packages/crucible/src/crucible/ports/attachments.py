"""What a person sent in a conversation, as the engine kept it — for a tool
that hands a person's file on (to an MCP server, say).

A conversation's files directory holds more than what people sent: a tool
writes there too, and the agent's confined process may overwrite anything in
it. So the engine keeps a record of each file as it arrived — name, size, a
digest — outside that directory, and a tool asks this port rather than trusting
a path: ``list`` says what the person sent, ``read`` hands over the bytes only
when the file is still the one recorded.
"""

from dataclasses import dataclass
from typing import Protocol

# What the common file formats start with: the type a file's bytes really are,
# against the one its name or the platform claims.
_MAGIC: tuple[tuple[bytes, str], ...] = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
    (b"%PDF-", "application/pdf"),
)


def sniff_content_type(data: bytes) -> str:
    """The media type these bytes really are, or "" when they are none this
    engine recognises (an image, a PDF)."""
    for magic, mime in _MAGIC:
        if data.startswith(magic):
            return mime
    # WebP is a RIFF container; the format only shows up at offset 8.
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return ""


@dataclass(frozen=True)
class ReceivedFile:
    """One file as it arrived with a message: where it was saved, what the
    sender called it, the type the platform claimed, its size and sha256."""

    path: str
    name: str
    mime: str
    size: int
    sha256: str
    received_at: str


class ReceivedFileGone(Exception):
    """The file is no longer where it was saved (retention, a pod restart)."""


class ReceivedFileChanged(Exception):
    """What is at the path is not the file that arrived: another size or
    digest, a symlink, a directory."""


class ReceivedFiles(Protocol):
    """The files one conversation received."""

    async def list(self) -> tuple[ReceivedFile, ...]:
        """What arrived, oldest first."""
        ...

    async def read(self, file: ReceivedFile) -> bytes:
        """The file's bytes, read once and checked against the record — raises
        ``ReceivedFileGone`` or ``ReceivedFileChanged`` rather than hand over
        anything else."""
        ...
