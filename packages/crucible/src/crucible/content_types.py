"""What a file's bytes really are, and how a size reads to a person — a leaf
with no engine in it, so a port's implementation, a flow and a tool can share
one answer."""

# What the common formats start with: the type a file's bytes really are,
# against the one its name or the platform claims.
_MAGIC: tuple[tuple[bytes, str], ...] = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
    (b"%PDF-", "application/pdf"),
)
# The types ``sniff_content_type`` can tell: for these, bytes decide; for any
# other type nobody can tell from the bytes alone.
KNOWN_CONTENT_TYPES = frozenset(mime for _, mime in _MAGIC) | {"image/webp"}


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


def sniff_image(data: bytes) -> str:
    """The image type these bytes really are, or "" if they are not an image."""
    found = sniff_content_type(data)
    return found if found.startswith("image/") else ""


def format_size(size: int) -> str:
    """A byte count as a person reads it: ``3 KB``, ``1.2 MB``."""
    if size >= 1024 * 1024:
        return f"{size / (1024 * 1024):.1f} MB"
    if size >= 1024:
        return f"{size / 1024:.0f} KB"
    return f"{size} B"
