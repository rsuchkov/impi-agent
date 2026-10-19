"""Captions for the runtime's own built-in tools, which are no classes of ours.

The deployment's typed tools caption themselves (``tools.base.Captioning``);
``read``, ``edit``, ``write``, ``grep``, ``find`` and ``ls`` are the runtime's,
and the driver is the one that knows their arguments. A ``bash`` call gets no
caption: its one argument is the command, and the command is exactly what a
caption must not show.
"""

from collections.abc import Mapping
from pathlib import PurePosixPath
from typing import Any

_VERBS = {"read": "Reading", "edit": "Editing", "write": "Writing", "ls": "Listing"}


def _name(value: Any) -> str:
    return PurePosixPath(str(value)).name if isinstance(value, str) and value else ""


class PiBuiltinCaptions:
    """``CallCaptions`` for the runtime's built-ins: the file's name, never its
    path or contents; the pattern a search is for; nothing for a command."""

    def caption(self, tool: str, args: Mapping[str, Any]) -> str:
        if tool in _VERBS:
            name = _name(args.get("path"))
            return f"{_VERBS[tool]} {name}" if name else ""
        if tool in ("grep", "find"):
            pattern = args.get("pattern")
            return f"Searching for {pattern}" if isinstance(pattern, str) and pattern else "Searching"
        return ""
