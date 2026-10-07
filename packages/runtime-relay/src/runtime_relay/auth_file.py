"""The runtime's credential file, checked before a process that reads it starts.

The same rule as the engine library's (``crucible.runtimes.pi.auth_file``),
kept here because this package does not depend on the library; a test in the
workspace holds the two to each other. A value the runtime would run as a
command (``!…``) is not one a login wrote.
"""

import json
import os
from pathlib import Path


def auth_file_path() -> Path:
    """Where this container's runtime reads its credential."""
    agent_dir = os.environ.get("PI_CODING_AGENT_DIR") or str(Path.home() / ".pi" / "agent")
    return Path(agent_dir) / "auth.json"


def executable_entries(path: Path) -> list[str]:
    """Entries of the credential file the runtime would run as a command."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    found: list[str] = []

    def walk(node: object, where: str) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                walk(value, f"{where}.{key}" if where else str(key))
        elif isinstance(node, list):
            for index, value in enumerate(node):
                walk(value, f"{where}[{index}]")
        elif isinstance(node, str) and node.startswith("!"):
            found.append(where)

    walk(data, "")
    return found
