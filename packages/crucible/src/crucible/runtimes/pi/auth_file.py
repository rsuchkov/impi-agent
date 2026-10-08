"""The runtime's credential file, checked before every process that reads it.

pi resolves a credential's value through its config-value rules, and a value
that starts with ``!`` is run as a shell command (its output is the key) — on
every model request, since it re-reads the file whenever it changed. The file
is shared by every runtime process, so whatever could write ``"key":
"!<anything>"`` into it would have that run in every other process reading it.
A confined process is therefore given the file to read only, and the
application refreshes the login (``auth_refresh.py``). What is left is a file
something unconfined wrote — no legitimate login writes such a value, so a
process is not started while the file holds one.

It is also the one place that knows the runtime's agent directory and what
the runtime keeps there.
"""

import json
import os
from pathlib import Path

# What the runtime keeps in its agent directory: the credential, the models it
# is told about, and the store of the catalogs it refreshes itself.
AUTH_FILE = "auth.json"
MODELS_FILE = "models.json"
MODELS_STORE_FILE = "models-store.json"


def runtime_agent_dir() -> Path:
    """This process's runtime's agent directory: ``PI_CODING_AGENT_DIR``,
    else ``~/.pi/agent``."""
    return Path(os.environ.get("PI_CODING_AGENT_DIR") or str(Path.home() / ".pi" / "agent"))


def auth_file_path() -> Path:
    """Where this process's runtime reads its credential."""
    return runtime_agent_dir() / AUTH_FILE


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


def configured_providers(agent_dir: Path | None = None, *, definitions: bool = True) -> frozenset[str]:
    """The providers the runtime has something for in its agent directory: a
    login or key in the credential file, and — with ``definitions`` — a
    definition in ``models.json``. Without them: what a process that is handed
    the credential file alone can use. A key the runtime would take from the
    environment is not seen here."""
    agent_dir = agent_dir or runtime_agent_dir()
    found: set[str] = set()
    sources = ((AUTH_FILE, None), (MODELS_FILE, "providers")) if definitions else ((AUTH_FILE, None),)
    for name, key in sources:
        try:
            data = json.loads((agent_dir / name).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        section = data.get(key) if key and isinstance(data, dict) else data
        if isinstance(section, dict):
            found.update(str(provider) for provider in section)
    return frozenset(found)
