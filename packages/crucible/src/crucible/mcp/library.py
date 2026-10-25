"""The MCP servers a deployment declares: one YAML each under ``_mcp/`` beside
the agents, and beside each the snapshot of its tools (``<name>.lock.json``).

A server entry says how to reach the server (a URL with headers, or a command
with arguments and an environment), how far to trust what it says about
itself, which of its tools a person must confirm, and what it is for. Secrets
are never in the file: a ``${NAME}`` in a header or an environment value is
filled from the mapping the application hands over — the engine's own
environment, where the ``MCP_`` family lives — and ``${connection:<provider>}``
in a header is left for the call, when it becomes the token of the person
whose turn it is.
"""

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import yaml

from crucible.mcp.protocol import CALL_TIMEOUT_S
from crucible.mcp.snapshot import SNAPSHOT_VERSION, Snapshot

MCP_DIR = "_mcp"
SERVER_FILE_SUFFIX = ".yaml"
SNAPSHOT_FILE_SUFFIX = ".lock.json"
TRUST_UNTRUSTED = "untrusted"  # definitions pinned; a server that changed them is not advertised
TRUST_TRUSTED = "trusted"  # the server's word is taken; the snapshot follows it
TRUST_LEVELS = (TRUST_UNTRUSTED, TRUST_TRUSTED)
# The one family of the engine's environment a server entry may name.
VALUES_FAMILY = "MCP_"
# Where a header asks for the person's own token at a connected provider.
CONNECTION_REF = re.compile(r"^\$\{connection:([A-Za-z0-9_-]+)\}$")
_VAR = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
# No double underscore: `mcp__<server>__<tool>` is split on the first one.
_NAME = re.compile(r"^[a-z0-9](?:_?[a-z0-9-])*$")


class McpConfigError(ValueError):
    """A server entry the engine cannot use, with the file and the reason."""


@dataclass(frozen=True)
class McpServer:
    name: str
    url: str = ""
    headers: Mapping[str, str] = field(default_factory=dict)
    command: str = ""
    args: tuple[str, ...] = ()
    env: Mapping[str, str] = field(default_factory=dict)
    cwd: str = ""
    trust: str = TRUST_UNTRUSTED
    confirm: tuple[str, ...] = ()  # tool-name patterns a person must confirm
    description: str = ""
    timeout_s: float = CALL_TIMEOUT_S  # how long one call to this server may take

    @property
    def is_http(self) -> bool:
        return bool(self.url)

    @property
    def trusted(self) -> bool:
        return self.trust == TRUST_TRUSTED

    def connection_providers(self) -> tuple[str, ...]:
        """The providers whose tokens the headers ask for, in header order."""
        found = []
        for value in self.headers.values():
            match = CONNECTION_REF.match(value)
            if match and match.group(1) not in found:
                found.append(match.group(1))
        return tuple(found)

    def to_yaml(self) -> dict[str, Any]:
        data: dict[str, Any] = {}
        if self.url:
            data["url"] = self.url
            if self.headers:
                data["headers"] = dict(self.headers)
        else:
            data["command"] = self.command
            if self.args:
                data["args"] = list(self.args)
            if self.env:
                data["env"] = dict(self.env)
            if self.cwd:
                data["cwd"] = self.cwd
        data["trust"] = self.trust
        if self.confirm:
            data["confirm"] = list(self.confirm)
        if self.description:
            data["description"] = self.description
        if self.timeout_s != CALL_TIMEOUT_S:
            data["timeout"] = self.timeout_s
        return data


def server_from_yaml(name: str, data: Mapping[str, Any], *, source: str = "") -> McpServer:
    where = source or name

    def strings(key: str) -> tuple[str, ...]:
        raw = data.get(key) or []
        if not isinstance(raw, list) or not all(isinstance(x, str) for x in raw):
            raise McpConfigError(f"{where}: {key} must be a list of strings")
        return tuple(raw)

    def mapping(key: str) -> dict[str, str]:
        raw = data.get(key) or {}
        if not isinstance(raw, Mapping) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in raw.items()
        ):
            raise McpConfigError(f"{where}: {key} must map names to strings")
        return dict(raw)

    if not _NAME.match(name):
        raise McpConfigError(
            f"{where}: {name!r} is not a server name (lowercase letters, digits, - and single _)"
        )
    url = str(data.get("url") or "")
    command = str(data.get("command") or "")
    if bool(url) == bool(command):
        raise McpConfigError(f"{where}: a server has either `url` or `command`")
    if url and not url.startswith(("http://", "https://")):
        raise McpConfigError(f"{where}: url must be http(s)")
    headers = mapping("headers")
    if url.startswith("http://") and any(CONNECTION_REF.match(v) for v in headers.values()):
        raise McpConfigError(f"{where}: a person's token (${{connection:…}}) goes only over https")
    trust = str(data.get("trust") or TRUST_UNTRUSTED)
    if trust not in TRUST_LEVELS:
        raise McpConfigError(f"{where}: trust must be one of {', '.join(TRUST_LEVELS)}")
    timeout = data.get("timeout", CALL_TIMEOUT_S)
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
        raise McpConfigError(f"{where}: timeout must be a number of seconds above zero")
    return McpServer(
        name=name, url=url, headers=headers, command=command, args=strings("args"),
        env=mapping("env"), cwd=str(data.get("cwd") or ""), trust=trust,
        confirm=strings("confirm"), description=str(data.get("description") or ""),
        timeout_s=float(timeout),
    )


def with_values(server: McpServer, values: Mapping[str, str]) -> McpServer:
    """The server with every ``${NAME}`` in its headers and environment filled
    from ``values``. Only the ``MCP_`` family is read: a server entry is a
    file in the agents directory, and a file must not be the way an engine's
    other secrets travel to an address of the file's choosing. A name
    ``values`` lacks is an error naming it, since a header sent with a hole
    would be a credential that is not there. A ``${connection:…}`` header is
    left as it is: that one is per call."""

    def fill(text: str, where: str) -> str:
        # A ``${connection:…}`` is not a variable (the colon keeps it out of
        # ``_VAR``) and passes through untouched, for the call.
        def one(match: re.Match[str]) -> str:
            name = match.group(1)
            if not name.startswith(VALUES_FAMILY):
                raise McpConfigError(
                    f"{server.name}: {where} names ${{{name}}}; only the {VALUES_FAMILY} family is read"
                )
            if name not in values:
                raise McpConfigError(f"{server.name}: {where} needs ${{{name}}}, which is not set")
            return values[name]

        return _VAR.sub(one, text)

    return replace(
        server,
        headers={k: fill(v, f"header {k}") for k, v in server.headers.items()},
        env={k: fill(v, f"env {k}") for k, v in server.env.items()},
    )


class McpLibrary:
    """The ``_mcp`` directory: servers and their snapshots, read and written."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    def list(self) -> list[McpServer]:
        """Every server declared, by name. A broken entry is an error: a server
        the operator wrote and the engine silently skipped is a server whose
        tools vanish with no word."""
        if not self.root.is_dir():
            return []
        servers = []
        for path in sorted(self.root.glob(f"*{SERVER_FILE_SUFFIX}")):
            servers.append(self._read(path))
        return servers

    def get(self, name: str) -> McpServer | None:
        path = self._server_path(name)
        return self._read(path) if path.is_file() else None

    def write(self, server: McpServer) -> Path:
        self.root.mkdir(parents=True, exist_ok=True)
        path = self._server_path(server.name)
        path.write_text(yaml.safe_dump(server.to_yaml(), sort_keys=False, allow_unicode=True), encoding="utf-8")
        return path

    def remove(self, name: str) -> bool:
        removed = False
        for path in (self._server_path(name), self._snapshot_path(name)):
            if path.is_file():
                path.unlink()
                removed = True
        return removed

    def snapshot(self, name: str) -> Snapshot | None:
        path = self._snapshot_path(name)
        if not path.is_file():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except ValueError as exc:
            raise McpConfigError(f"{path}: not JSON") from exc
        if not isinstance(data, Mapping):
            raise McpConfigError(f"{path}: not a snapshot")
        if data.get("version") != SNAPSHOT_VERSION:
            raise McpConfigError(f"{path}: snapshot version {data.get('version')!r}; this engine writes {SNAPSHOT_VERSION}")
        return Snapshot.from_json(data)

    def save_snapshot(self, snapshot: Snapshot) -> Path:
        self.root.mkdir(parents=True, exist_ok=True)
        path = self._snapshot_path(snapshot.server)
        path.write_text(json.dumps(snapshot.to_json(), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        return path

    def _server_path(self, name: str) -> Path:
        return self.root / f"{name}{SERVER_FILE_SUFFIX}"

    def _snapshot_path(self, name: str) -> Path:
        return self.root / f"{name}{SNAPSHOT_FILE_SUFFIX}"

    def _read(self, path: Path) -> McpServer:
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as exc:
            raise McpConfigError(f"{path}: {exc}") from exc
        if not isinstance(data, Mapping):
            raise McpConfigError(f"{path}: not a mapping")
        return server_from_yaml(path.name[: -len(SERVER_FILE_SUFFIX)], data, source=str(path))
