"""impi's chat-management typed tools: the multi-agent choreography surface
(list agents, assemble channels, hand work to another agent and read its reply).
They register additively into crucible's default tool registry via ``@tool`` and
depend only on ports (directory, chat-admin). Importing this module runs the
decorators, so the app imports it before building the registry."""

import logging
from typing import Any, ClassVar

from pydantic_settings import BaseSettings, SettingsConfigDict

from crucible.tools.base import CAP_CHAT_ADMIN, Tool, ToolContext, ToolError
from crucible.tools.registry import tool

logger = logging.getLogger(__name__)


def _require_str(args: dict[str, Any], key: str) -> str:
    value = args.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ToolError(f"missing required string argument {key!r}")
    return value.strip()


@tool
class ListAgents(Tool):
    name: ClassVar[str] = "list_agents"
    description: ClassVar[str] = (
        "List the other agents in this system with their roles, so you can decide "
        "whom to involve. Returns name, role, description and @username for each."
    )
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    }

    async def execute(self, ctx: ToolContext, args: dict[str, Any]) -> Any:
        return {
            "agents": [
                {
                    "name": a.name,
                    "role": a.role,
                    "description": a.description,
                    "username": a.username,
                }
                for a in ctx.directory.list_agents()
            ]
        }


class CreateChannelSettings(BaseSettings):
    """create_channel's own env-bound settings — declared here, loaded generically
    by the registry (env: TOOL_CREATE_CHANNEL_*). Adding this needs no app.py edit.
    Read from .env via pydantic (not os.environ), so secrets never reach the
    runtime subprocess."""

    model_config = SettingsConfigDict(
        env_prefix="TOOL_CREATE_CHANNEL_", env_file=".env", extra="ignore"
    )

    auto_invite_owner: bool = True  # add the human owner to private channels...
    owner_username: str = ""  # ...this login; empty disables it


@tool
class CreateChannel(Tool):
    name: ClassVar[str] = "create_channel"
    requires: ClassVar[frozenset[str]] = frozenset({CAP_CHAT_ADMIN})
    settings_cls: ClassVar[type | None] = CreateChannelSettings
    description: ClassVar[str] = (
        "Create a Mattermost channel (private by default) that you own, e.g. to "
        "gather people and agents around a task. Returns the channel_id — use it "
        "with invite_to_channel."
    )
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "display_name": {"type": "string", "description": "Human-readable channel name"},
            "name": {"type": "string", "description": "URL slug; derived from display_name if omitted"},
            "private": {"type": "boolean", "description": "Private channel (default true)"},
            "purpose": {"type": "string", "description": "Short channel purpose"},
        },
        "required": ["display_name"],
    }

    async def execute(self, ctx: ToolContext, args: dict[str, Any]) -> Any:
        display_name = _require_str(args, "display_name")
        name = str(args.get("name") or display_name)
        private = bool(args.get("private", True))
        purpose = str(args.get("purpose") or "")
        channel_id = await ctx.require_chat_admin().create_channel(
            name, display_name, private=private, purpose=purpose
        )
        result: dict[str, Any] = {"channel_id": channel_id}
        # A private channel the agent made would be invisible to the human;
        # add the owner so they can see and join the conversation.
        cfg = ctx.settings if isinstance(ctx.settings, CreateChannelSettings) else CreateChannelSettings()
        if private and cfg.auto_invite_owner and cfg.owner_username:
            owner_id = await ctx.require_chat_admin().resolve_username(cfg.owner_username)
            if owner_id:
                await ctx.require_chat_admin().invite_to_channel(channel_id, owner_id)
                result["owner_invited"] = True
            else:
                logger.warning("owner %r not found; not auto-invited", cfg.owner_username)
                result["owner_invited"] = False
        return result


@tool
class InviteToChannel(Tool):
    name: ClassVar[str] = "invite_to_channel"
    requires: ClassVar[frozenset[str]] = frozenset({CAP_CHAT_ADMIN})
    description: ClassVar[str] = (
        "Invite a user or another agent into a channel. `target` may be an agent "
        "name, an @username, or a user id."
    )
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "channel_id": {"type": "string"},
            "target": {"type": "string", "description": "agent name, @username, or user id"},
        },
        "required": ["channel_id", "target"],
    }

    async def execute(self, ctx: ToolContext, args: dict[str, Any]) -> Any:
        channel_id = _require_str(args, "channel_id")
        target = _require_str(args, "target")
        user_id = await self._resolve(ctx, target)
        if user_id is None:
            raise ToolError(f"could not resolve {target!r} to a user")
        await ctx.require_chat_admin().invite_to_channel(channel_id, user_id)
        return {"invited": user_id}

    @staticmethod
    async def _resolve(ctx: ToolContext, target: str) -> str | None:
        for agent in ctx.directory.list_agents():
            if target in (agent.name, agent.username):
                return agent.user_id
        return await ctx.require_chat_admin().resolve_username(target)


@tool
class GetChannelMembers(Tool):
    name: ClassVar[str] = "get_channel_members"
    requires: ClassVar[frozenset[str]] = frozenset({CAP_CHAT_ADMIN})
    description: ClassVar[str] = "List the members (user_id + username) of a channel."
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {"channel_id": {"type": "string"}},
        "required": ["channel_id"],
    }

    async def execute(self, ctx: ToolContext, args: dict[str, Any]) -> Any:
        channel_id = _require_str(args, "channel_id")
        members = await ctx.require_chat_admin().get_channel_members(channel_id)
        return {
            "members": [{"user_id": m.user_id, "username": m.username} for m in members]
        }


# An agent-initiated message seeds the cascade counter one hop in, so an
# agent->agent exchange it starts stays bounded by LoopGuard's hop cap. The
# per-conversation rate limit is the hard backstop regardless of this value.
_AGENT_MESSAGE_HOP = 1


@tool
class SendMessage(Tool):
    name: ClassVar[str] = "send_message"
    requires: ClassVar[frozenset[str]] = frozenset({CAP_CHAT_ADMIN})
    description: ClassVar[str] = (
        "Post a message to a channel by its channel_id (e.g. one you created). "
        "Posts as you, at the top level of the channel. To involve another agent, "
        "@mention it in the message (by @username) — it will pick the message up "
        "and reply in that channel; use read_channel to see the reply. You must be "
        "a member of the channel."
    )
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "channel_id": {"type": "string"},
            "message": {"type": "string", "description": "Markdown message to post"},
        },
        "required": ["channel_id", "message"],
    }

    async def execute(self, ctx: ToolContext, args: dict[str, Any]) -> Any:
        channel_id = _require_str(args, "channel_id")
        message = _require_str(args, "message")
        try:
            post_id = await ctx.require_chat_admin().post_message(
                channel_id, message, hop_depth=_AGENT_MESSAGE_HOP
            )
        except Exception as exc:  # e.g. not a member of the channel (MM 403)
            raise ToolError(f"could not post to that channel (are you a member?): {exc}") from exc
        return {"posted": True, "post_id": post_id}


@tool
class ReadChannel(Tool):
    name: ClassVar[str] = "read_channel"
    requires: ClassVar[frozenset[str]] = frozenset({CAP_CHAT_ADMIN})
    description: ClassVar[str] = (
        "Read the most recent messages in a channel by its channel_id (e.g. to see "
        "another agent's reply after send_message). Returns author + text, oldest "
        "first. You must be a member of the channel."
    )
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "channel_id": {"type": "string"},
            "limit": {
                "type": "integer",
                "description": "How many recent messages to return (1-50, default 20)",
            },
        },
        "required": ["channel_id"],
    }

    async def execute(self, ctx: ToolContext, args: dict[str, Any]) -> Any:
        channel_id = _require_str(args, "channel_id")
        limit = args.get("limit", 20)
        if not isinstance(limit, int) or not (1 <= limit <= 50):
            limit = 20
        posts = await ctx.require_chat_admin().get_channel_posts(channel_id, limit)
        return {"messages": [{"author": p.username, "text": p.text} for p in posts]}
