"""ToolRegistry + a ``@tool`` decorator to add tools without touching a list.

Adding a tool: define a class in any module the composition root imports and
decorate it with ``@tool``. It self-registers; the manifest and the tool
extension pick it up automatically. The framework bundles no tools of its own —
see ``crucible.builtin_tools`` for the generic ones.
"""

import json
from pathlib import Path
from typing import Any

from crucible.tools.base import SPEAKS_TO_USER_NOTE, Tool

_registered: list[Tool] = []


def tool(cls: type[Tool]) -> type[Tool]:
    """Register a tool class (instantiated once) into the default registry."""
    _registered.append(cls())
    return cls


class ToolRegistry:
    def __init__(self, tools: tuple[Tool, ...]) -> None:
        self._tools: dict[str, Tool] = {t.name: t for t in tools}

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return sorted(self._tools)

    def load_configs(self, env_file: str = ".env") -> dict[str, Any]:
        """Instantiate the settings of every tool that declares one (env-bound).
        Generic — a new configured tool self-registers here, no wiring edits.

        ``env_file`` is where each tool's BaseSettings reads .env from; passing a
        non-existent path (tests) makes them fall back to defaults. Reading .env
        via pydantic keeps secrets out of os.environ (and thus out of the runtime
        subprocesses)."""
        configs: dict[str, Any] = {}
        for name, t in self._tools.items():
            cls = t.settings_cls
            if cls is not None:
                configs[name] = cls(_env_file=env_file)  # type: ignore[call-arg]
        return configs

    def manifest(self, allowed: tuple[str, ...]) -> list[dict[str, Any]]:
        """The declaration the tool extension registers, for an agent's allowed
        tools (unknown names are skipped)."""
        entries: list[dict[str, Any]] = []
        for name in allowed:
            t = self._tools.get(name)
            if t is not None:
                description = t.description
                if t.speaks_to_user:
                    # Generated, never hand-written: one wording for every such
                    # tool, and a new one cannot be forgotten. A description that
                    # says this itself is the drift this replaces.
                    description = f"{description} {SPEAKS_TO_USER_NOTE}"
                entries.append(
                    {
                        "name": t.name,
                        "description": description,
                        "parameters": t.parameters,
                        "requires_confirmation": t.requires_confirmation,
                        "speaks_to_user": t.speaks_to_user,
                    }
                )
        return entries

    def write_manifest(self, dir_path: Path, agent: str, allowed: tuple[str, ...]) -> Path:
        """Persist an agent's manifest to ``<dir>/<agent>.json`` (the tool
        extension reads it synchronously) and return the resolved path. The path
        is stable per agent, so a hot-reload can rewrite it in place."""
        dir_path.mkdir(parents=True, exist_ok=True)
        path = dir_path / f"{agent}.json"
        path.write_text(json.dumps(self.manifest(allowed), ensure_ascii=False), encoding="utf-8")
        return path.resolve()


def build_registry() -> ToolRegistry:
    """Snapshot every tool registered via @tool so far. The framework bundles no
    tools of its own — the composition root imports the tool modules it wants
    (``crucible.builtin_tools`` for the generic ask/form tools, plus any
    app-specific modules) before calling this."""
    return ToolRegistry(tuple(_registered))
