"""The runtime's credential file, checked before every process that reads it.

pi resolves a credential's value through its config-value rules, and a value
that starts with ``!`` is run as a shell command (its output is the key). The
file is shared: the engine reads it, and every agent container is handed it
read-write so its runtime can refresh the token in place. An agent with a shell
could therefore write ``"key": "!<anything>"`` into it and have that run in
the engine's process and in every other agent's, as their user, on their next
spawn. No legitimate login writes such a value, so a process is not started
while the file holds one.
"""

import json
import os
from pathlib import Path


def auth_file_path() -> Path:
    """Where this process's runtime reads its credential: the runtime's agent
    directory (``PI_CODING_AGENT_DIR``, else ``~/.pi/agent``)."""
    agent_dir = os.environ.get("PI_CODING_AGENT_DIR") or str(Path.home() / ".pi" / "agent")
    return Path(agent_dir) / "auth.json"


def executable_entries(path: Path) -> list[str]:
    """The entries of the credential file whose value the runtime would run as
    a command, by dotted path. Empty when there is no file, or nothing there
    parses — the runtime then has nothing to run either."""
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
