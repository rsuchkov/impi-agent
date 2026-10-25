"""Where the engine keeps tool manifests, and nothing about what is in them.

A manifest is the list of tools a runtime process advertises to its model,
read by the tool extension when the process starts. The agent's own is
``<manifests>/<agent>.json``; a conversation entitled to less than its agent
gets ``<manifests>/<agent>/<runtime session id>.json`` beside it, keyed the way
the runtime keys the conversation's memory. The registry writes them; this leaf
says where they are, so what forgets a conversation (``session_retention``,
``sessions_cli``) can take its manifest with it without the tool layer.
"""

import re
from pathlib import Path

MANIFESTS_DIR_NAME = "tool-manifests"
# The one pattern an agent's tool list may carry: every tool of one declared
# MCP server, ``mcp__<server>__*`` (a server name is lowercase letters, digits,
# hyphens and single underscores — ``crucible.mcp.library``). Read by the
# profile loader, which may not import the MCP package.
MCP_SERVER_TOOLS = re.compile(r"^mcp__[a-z0-9](?:_?[a-z0-9-])*__\*$")
# The env variable naming the manifest FILE a process reads: the engine ↔ tool
# extension contract. A name rather than a literal at the call site because a
# composition root has to tell it apart from the rest of the env: it is a
# path, and a path only means something where it was written.
MANIFEST_ENV = "TOOL_MANIFEST"


def manifests_dir(data_dir: Path | str) -> Path:
    """The manifests under an application's data directory."""
    return Path(data_dir) / MANIFESTS_DIR_NAME


def manifest_path(dir_path: Path, agent: str) -> Path:
    """An agent's manifest. Stable per agent, so a hot-reload rewrites it in
    place and a process started later reads the current one."""
    return (dir_path / f"{agent}.json").resolve()


def session_manifest_path(dir_path: Path, agent: str, runtime_session_id: str) -> Path:
    """One conversation's manifest. The id is the store's, drawn from
    ``[A-Za-z0-9._-]``; anything else is refused rather than made a path of."""
    if not runtime_session_id or "/" in runtime_session_id or runtime_session_id in (".", ".."):
        raise ValueError(f"not a runtime session id: {runtime_session_id!r}")
    return (dir_path / agent / f"{runtime_session_id}.json").resolve()


def forget_session_manifest(dir_path: Path, agent: str, runtime_session_id: str) -> bool:
    """Remove a conversation's manifest, when it had one of its own. Called
    where the conversation's memory is forgotten; a conversation that is reset
    and goes on keeps it, since its next turn rewrites it."""
    try:
        path = session_manifest_path(dir_path, agent, runtime_session_id)
    except ValueError:
        return False
    try:
        path.unlink()
    except FileNotFoundError:
        return False
    return True
