"""What a server's tools were when somebody looked: the definitions, pinned.

A tool's description is read by the model as an instruction, so the engine
advertises the definitions it was shown when the server was added (or last
reviewed), not whatever the server says today. The digest is over the fields
that reach the model, in a canonical order, so a changed description is a
changed digest and a reordered list is not.
"""

import hashlib
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from crucible.mcp.protocol import ToolDefinition

SNAPSHOT_VERSION = 1


def canonical(tools: Iterable[ToolDefinition]) -> str:
    entries = sorted((t.to_wire() for t in tools), key=lambda t: t["name"])
    return json.dumps(entries, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def digest(tools: Iterable[ToolDefinition]) -> str:
    return hashlib.sha256(canonical(tools).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Snapshot:
    server: str
    tools: tuple[ToolDefinition, ...]
    digest: str
    taken_at: str  # ISO 8601 UTC
    server_info: Mapping[str, Any]

    @classmethod
    def taken(
        cls, server: str, tools: Iterable[ToolDefinition], taken_at: str,
        server_info: Mapping[str, Any] | None = None,
    ) -> "Snapshot":
        pinned = tuple(sorted(tools, key=lambda t: t.name))
        return cls(server, pinned, digest(pinned), taken_at, dict(server_info or {}))

    def to_json(self) -> dict[str, Any]:
        return {
            "version": SNAPSHOT_VERSION,
            "server": self.server,
            "taken_at": self.taken_at,
            "digest": self.digest,
            "server_info": dict(self.server_info),
            "tools": [t.to_wire() for t in self.tools],
        }

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> "Snapshot":
        """The snapshot as the file holds it. The digest is computed from the
        tools in the file, not read from it: what the model will be shown is
        the file's tools, so that is what the live server is compared with — a
        file whose text was edited by hand is judged by its text."""
        tools = tuple(ToolDefinition.from_wire(t) for t in data.get("tools") or () if isinstance(t, Mapping))
        return cls(
            server=str(data.get("server", "")),
            tools=tools,
            digest=digest(tools),
            taken_at=str(data.get("taken_at", "")),
            server_info=dict(data.get("server_info") or {}),
        )

    def differs_from(self, tools: Iterable[ToolDefinition]) -> bool:
        return digest(tools) != self.digest
