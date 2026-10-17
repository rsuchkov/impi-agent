"""ToolRegistry: the tools a deployment offers, built from the modules its
composition root names.

A tool is a ``Tool`` class marked ``@tool`` in the module that defines it. The
mark registers nothing: it says this class is a tool the root may list, as
opposed to a base or a helper that also subclasses ``Tool``. The root names
the modules — ``build_registry(builtin_tools, my_tools)`` — and the registry
is what those modules mark, plus any tool the root configured itself (an
instance built from a definition at startup, passed in a list). Nothing
registers by being imported: a forgotten module is a missing name in that
call rather than a tool that quietly vanished, and a test's registry does not
depend on what else was imported before it. The framework bundles no tools of
its own — see ``crucible.builtin_tools`` for the generic ones.
"""

import json
from collections.abc import Iterable
from pathlib import Path
from types import ModuleType
from typing import Any, TypeVar, cast

from crucible.private_files import private_dir, write_private
from crucible.tool_manifests import manifest_path, session_manifest_path
from crucible.tools.base import SPEAKS_TO_USER_NOTE, UNTRUSTED_NOTE, Tool

_ToolClass = TypeVar("_ToolClass", bound=type[Tool])

# The mark ``@tool`` leaves on a class; what ``build_registry`` looks for in a
# module. A dunder so a subclass does not inherit a mark it did not ask for
# — the check reads the class's own namespace, not its MRO.
_TOOL_MARK = "__crucible_tool__"


def tool(cls: _ToolClass) -> _ToolClass:
    """Mark a tool class for ``build_registry`` to collect from its module.
    Returns the class as it was, so what it declares beyond the port — a
    ``describe``, say — stays visible to a caller holding it by name."""
    setattr(cls, _TOOL_MARK, True)
    return cls


def marked_tools(module: ModuleType) -> list[type[Tool]]:
    """The ``@tool`` classes a module defines, in definition order — not one
    it imported from elsewhere, so a re-export does not count twice."""
    # The mark alone says it is a tool: ``Tool`` is a protocol with attributes,
    # which ``issubclass`` cannot check, and a marked class promises the port.
    return [
        cast(type[Tool], obj) for obj in vars(module).values()
        if isinstance(obj, type) and _TOOL_MARK in vars(obj) and obj.__module__ == module.__name__
    ]


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
                if t.returns_untrusted:
                    description = f"{description} {UNTRUSTED_NOTE}"
                entries.append(
                    {
                        "name": t.name,
                        "description": description,
                        "parameters": t.parameters,
                        "speaks_to_user": t.speaks_to_user,
                        "returns_untrusted": t.returns_untrusted,
                    }
                )
        return entries

    def write_manifest(self, dir_path: Path, agent: str, allowed: tuple[str, ...]) -> Path:
        """Persist an agent's manifest to ``<dir>/<agent>.json`` (the tool
        extension reads it synchronously) and return the resolved path. The path
        is stable per agent, so a hot-reload can rewrite it in place."""
        # The owner's alone: it says what each agent may call, and nobody else
        # on the machine needs to read that.
        private_dir(dir_path)
        return self._write(manifest_path(dir_path, agent), allowed)

    def write_session_manifest(
        self, dir_path: Path, agent: str, runtime_session_id: str, allowed: tuple[str, ...]
    ) -> Path:
        """Persist one conversation's manifest to ``<dir>/<agent>/<session>.json``:
        what that conversation's process advertises, rewritten before every
        turn it may start a process for."""
        path = session_manifest_path(dir_path, agent, runtime_session_id)
        private_dir(path.parent)
        return self._write(path, allowed)

    def _write(self, path: Path, allowed: tuple[str, ...]) -> Path:
        write_private(path, json.dumps(self.manifest(allowed), ensure_ascii=False))
        return path


def build_registry(*sources: ModuleType | Iterable[Tool | type[Tool]]) -> ToolRegistry:
    """The registry over what the composition root names: a module, for the
    tools marked ``@tool`` in it; or a list of tools — each a class,
    instantiated once here, or an instance the root configured itself. Two
    tools with one name is a mistake in the sources, said at startup rather
    than resolved by order."""
    instances: list[Tool] = []
    seen: set[str] = set()
    for source in sources:
        entries = marked_tools(source) if isinstance(source, ModuleType) else source
        for entry in entries:
            instance = entry() if isinstance(entry, type) else entry
            if instance.name in seen:
                raise ValueError(f"two tools are called {instance.name!r}")
            seen.add(instance.name)
            instances.append(instance)
    return ToolRegistry(tuple(instances))
