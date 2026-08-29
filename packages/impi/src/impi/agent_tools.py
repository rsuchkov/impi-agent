"""The support agent's create_agent engine tool: provision a Mattermost bot,
scaffold the agent profile, and record its token in the engine .env — the
chat-side twin of `impi agent add`, sharing the same provisioning core.

Registered globally (``@tool``) but named only in the bundled support agent's
allowlist; a hard in-handler guard keeps any other caller out even if a user
profile lists it. The confirmation flag makes the runtime ask the operator in
chat before the tool runs.
"""

import shutil
from pathlib import Path
from typing import Any, ClassVar

from crucible.tools.base import Tool, ToolContext, ToolError
from crucible.tools.registry import tool
from impi.provisioning import (
    AGENT_NAME_RE,
    CreateAgentSettings,
    ProvisioningError,
    agent_env_key,
    provision_mm_bot,
    set_env_key,
    write_agent_profile,
)

_SUPPORT_AGENT = "support"


@tool
class CreateAgent(Tool):
    name: ClassVar[str] = "create_agent"
    requires_confirmation: ClassVar[bool] = True
    settings_cls: ClassVar[type | None] = CreateAgentSettings
    description: ClassVar[str] = (
        "Create a brand-new agent end to end: provision its Mattermost bot "
        "account, scaffold the profile under the agents directory, and store "
        "its token in the engine .env. Use this instead of asking the operator "
        "to create bots by hand; refine .pi/SYSTEM.md with the file tools "
        "afterwards. The new agent does not start on its own: the result says "
        "exactly which command the operator has to run, and it differs by "
        "deployment — pass it on verbatim rather than guessing."
    )
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "Agent slug (lowercase letters, digits, hyphens); "
                "also the bot's @username and the profile directory name",
            },
            "role": {"type": "string", "description": "Short role, e.g. language-tutor"},
            "display_name": {"type": "string", "description": "Human-readable name"},
            "description": {"type": "string", "description": "One-line description"},
            "system_prompt": {
                "type": "string",
                "description": "Initial SYSTEM.md content (any language); omit "
                "for a neutral default you can edit later",
            },
        },
        "required": ["name", "role"],
    }

    async def execute(self, ctx: ToolContext, args: dict[str, Any]) -> Any:
        if ctx.agent_name != _SUPPORT_AGENT:
            raise ToolError("create_agent is restricted to the support agent")
        cfg = (
            ctx.settings
            if isinstance(ctx.settings, CreateAgentSettings)
            else CreateAgentSettings()
        )
        if not cfg.admin_token:
            raise ToolError(
                "no admin token configured — the operator must set "
                "TOOL_CREATE_AGENT_ADMIN_TOKEN in the engine .env"
            )
        if not cfg.mattermost_url or not cfg.agents_path:
            raise ToolError(
                "provisioning is not configured (MATTERMOST_URL / AGENTS_PATH missing)"
            )
        name = str(args.get("name") or "").strip()
        role = str(args.get("role") or "").strip()
        if not AGENT_NAME_RE.match(name):
            raise ToolError(
                f"invalid agent name {name!r}: lowercase letters, digits, hyphens"
            )
        if not role:
            raise ToolError("role must not be empty")
        display_name = str(args.get("display_name") or "").strip() or name
        description = str(args.get("description") or "").strip()
        system_prompt = str(args.get("system_prompt") or "")

        try:
            profile_dir = write_agent_profile(
                Path(cfg.agents_path),
                name=name,
                role=role,
                display_name=display_name,
                description=description,
                system_prompt=system_prompt,
            )
        except ProvisioningError as exc:
            raise ToolError(str(exc)) from exc
        try:
            creds = await provision_mm_bot(
                cfg.mattermost_url,
                cfg.admin_token,
                username=name,
                display_name=display_name,
                description=description,
                team=cfg.team,
            )
            set_env_key(cfg.dotenv_path, agent_env_key(name), creds.token)
            if cfg.gateway and cfg.gateway != "mattermost":
                set_env_key(cfg.dotenv_path, agent_env_key(name, "GATEWAY"), "mattermost")
        except ProvisioningError as exc:
            # No orphan profile without a bot: undo the scaffold we just wrote.
            shutil.rmtree(profile_dir, ignore_errors=True)
            raise ToolError(str(exc)) from exc
        if cfg.agent_hosts_enabled:
            # Each agent runs in a container of its own here, and building one is
            # not something this process can do — nor should be: reaching the
            # container runtime from in here would undo the separation the
            # containers exist to create.
            hint = (
                f"ask the operator to run `impi agent sync` on the host — it "
                f"builds {name} a container of its own and starts it. Nothing "
                f"happens until they do."
            )
        else:
            hint = (
                "ask the operator to restart the engine (`impi restart`, or "
                "`make stop && make run-bg` in a dev checkout)"
            )
        return {
            "created": True,
            "username": creds.username,
            "team": creds.team,
            "profile": str(profile_dir),
            "restart_required": True,
            "hint": hint,
        }
