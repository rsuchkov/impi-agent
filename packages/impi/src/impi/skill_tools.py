"""The support agent's skill-registry tools: browse the shared library, install a
skill, and give it to an agent — the chat-side twin of `impi skill`, sharing the
same core (``crucible.skills``).

Same shape as create_agent: registered globally, named only in the bundled
support agent's allowlist, with a hard in-handler guard. Installing runs someone
else's scripts inside the engine, so it asks for confirmation in chat first.
"""

import os
import shutil
import signal
from pathlib import Path
from typing import Any, ClassVar

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from crucible.skills import (
    SkillError,
    SkillLibrary,
    assign_skill,
    assigned_skills,
    declared_tools,
    install,
    stage,
    unassign_skill,
)
from crucible.tools.base import Tool, ToolContext, ToolError
from crucible.tools.registry import tool

_SUPPORT_AGENT = "support"


class SkillSettings(BaseSettings):
    """Where the library and the profiles live (env: TOOL_SKILL_*), falling back
    to the engine's own keys so a normal deployment configures nothing extra."""

    model_config = SettingsConfigDict(
        env_prefix="TOOL_SKILL_", env_file=".env", extra="ignore", populate_by_name=True
    )

    skills_path: str = Field(
        default="", validation_alias=AliasChoices("TOOL_SKILL_SKILLS_PATH", "SKILLS_PATH")
    )
    agents_path: str = Field(
        default="", validation_alias=AliasChoices("TOOL_SKILL_AGENTS_PATH", "AGENTS_PATH")
    )

    def library(self) -> SkillLibrary:
        root = self.skills_path or str(Path(self.agents_path or ".") / "_skills")
        return SkillLibrary(root)

    def manifest(self, agent: str) -> Path:
        if not self.agents_path:
            raise ToolError("no agents directory configured (AGENTS_PATH)")
        manifest = Path(self.agents_path) / "agents" / agent / "agent.yaml"
        if not manifest.is_file():
            raise ToolError(f"no agent {agent!r} (looked for {manifest})")
        return manifest


def _config(ctx: ToolContext) -> SkillSettings:
    if ctx.agent_name != _SUPPORT_AGENT:
        raise ToolError("the skill-library tools are restricted to the support agent")
    return ctx.settings if isinstance(ctx.settings, SkillSettings) else SkillSettings()


def _reload_engine() -> bool:
    """Ask the engine to re-read the profiles. The tool server runs inside it, so
    signalling ourselves reaches the same handler as `impi reload`; without this
    an assignment would only take effect after a restart.

    Signalled ONLY when a handler is actually installed: SIGHUP's default action
    is to kill the process, and this same code runs in tests and one-shot CLI
    invocations that never registered one."""
    if signal.getsignal(signal.SIGHUP) in (signal.SIG_DFL, signal.SIG_IGN, None):
        return False
    try:
        os.kill(os.getpid(), signal.SIGHUP)
    except OSError:
        return False
    return True


@tool
class ListSkills(Tool):
    name: ClassVar[str] = "list_skills"
    settings_cls: ClassVar[type | None] = SkillSettings
    description: ClassVar[str] = (
        "List the shared skill library: what is installed, what each skill does, "
        "where it came from, and which agents already have it. Use before "
        "installing (it may be there already) or when asked what an agent can do."
    )
    parameters: ClassVar[dict[str, Any]] = {"type": "object", "properties": {}}

    async def execute(self, ctx: ToolContext, args: dict[str, Any]) -> Any:
        cfg = _config(ctx)
        library = cfg.library()
        users: dict[str, list[str]] = {}
        if cfg.agents_path:
            for manifest in sorted(Path(cfg.agents_path).glob("agents/*/agent.yaml")):
                for name in assigned_skills(manifest):
                    users.setdefault(name, []).append(manifest.parent.name)
        return {
            "library": str(library.root),
            "skills": [
                {
                    "name": s.name,
                    "description": s.description,
                    "version": s.version,
                    "requires_tools": list(s.requires_tools),
                    "source": s.source.describe() if s.source else "local",
                    "assigned_to": users.get(s.name, []),
                }
                for s in library.list()
            ],
        }


@tool
class InstallSkill(Tool):
    name: ClassVar[str] = "install_skill"
    requires_confirmation: ClassVar[bool] = True
    settings_cls: ClassVar[type | None] = SkillSettings
    description: ClassVar[str] = (
        "Install a skill into the shared library from a directory on disk or a "
        "repository (owner/repo[/path][@ref], or a git URL[#path][@ref]). Copies "
        "the files in and records where they came from. This does NOT give the "
        "skill to any agent — call assign_skill for that."
    )
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "source": {
                "type": "string",
                "description": "Directory path, owner/repo[/path][@ref], or git URL",
            },
            "name": {
                "type": "string",
                "description": "Install under this name (default: the skill's own)",
            },
            "force": {"type": "boolean", "description": "Overwrite an installed skill"},
        },
        "required": ["source"],
    }

    async def execute(self, ctx: ToolContext, args: dict[str, Any]) -> Any:
        cfg = _config(ctx)
        source = str(args.get("source") or "").strip()
        if not source:
            raise ToolError("source must not be empty")
        try:
            with stage(source) as staged:
                files = [
                    {"path": path, "bytes": size, "executable": executable}
                    for path, size, executable in staged.files()
                ]
                skill = install(
                    cfg.library(), staged,
                    name=str(args.get("name") or "").strip(),
                    force=bool(args.get("force")),
                )
        except SkillError as exc:
            raise ToolError(str(exc)) from exc
        return {
            "installed": skill.name,
            "description": skill.description,
            "path": str(skill.path),
            "source": skill.source.describe() if skill.source else source,
            "requires_tools": list(skill.requires_tools),
            # What was copied — the operator should be able to see what will run.
            "files": files,
        }


@tool
class AssignSkill(Tool):
    name: ClassVar[str] = "assign_skill"
    settings_cls: ClassVar[type | None] = SkillSettings
    description: ClassVar[str] = (
        "Give an installed skill to an agent (or take it away with remove=true) "
        "by editing that agent's profile, then reload so it applies on the "
        "agent's next turn. The skill must already be in the library."
    )
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "skill": {"type": "string", "description": "Skill name in the library"},
            "agent": {"type": "string", "description": "Agent that should get it"},
            "remove": {"type": "boolean", "description": "Unassign instead of assign"},
        },
        "required": ["skill", "agent"],
    }

    async def execute(self, ctx: ToolContext, args: dict[str, Any]) -> Any:
        cfg = _config(ctx)
        skill_name = str(args.get("skill") or "").strip()
        agent = str(args.get("agent") or "").strip()
        if not skill_name or not agent:
            raise ToolError("skill and agent must not be empty")
        manifest = cfg.manifest(agent)
        if args.get("remove"):
            changed = unassign_skill(manifest, skill_name)
            return {"agent": agent, "skill": skill_name, "assigned": False,
                    "changed": changed, "reloaded": _reload_engine()}
        try:
            skill = cfg.library().get(skill_name)
        except SkillError as exc:
            raise ToolError(str(exc)) from exc
        missing = [t for t in skill.requires_tools if t not in declared_tools(manifest)]
        changed = assign_skill(manifest, skill.name)
        result: dict[str, Any] = {
            "agent": agent, "skill": skill.name, "assigned": True,
            "changed": changed, "reloaded": _reload_engine(),
        }
        if missing:
            # The skill is assigned but cannot actually run: say so rather than
            # let it fail silently mid-turn.
            result["warning"] = (
                f"{agent} does not allow the tools this skill needs: "
                f"{', '.join(missing)} — add them to runtime.tools in {manifest}"
            )
        return result


@tool
class RemoveSkill(Tool):
    name: ClassVar[str] = "remove_skill"
    requires_confirmation: ClassVar[bool] = True
    settings_cls: ClassVar[type | None] = SkillSettings
    description: ClassVar[str] = (
        "Delete a skill from the shared library. Refuses while an agent still "
        "references it — unassign it there first."
    )
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {"name": {"type": "string", "description": "Skill name"}},
        "required": ["name"],
    }

    async def execute(self, ctx: ToolContext, args: dict[str, Any]) -> Any:
        cfg = _config(ctx)
        name = str(args.get("name") or "").strip()
        library = cfg.library()
        if not library.has(name):
            raise ToolError(f"no skill {name!r} in {library.root}")
        users = [
            manifest.parent.name
            for manifest in sorted(Path(cfg.agents_path or ".").glob("agents/*/agent.yaml"))
            if name in assigned_skills(manifest)
        ]
        if users:
            raise ToolError(f"{name} is still assigned to: {', '.join(users)}")
        shutil.rmtree(library.path_of(name))
        return {"removed": name}
