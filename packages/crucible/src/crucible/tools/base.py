"""Tool port + the context one receives when it runs.

A tool is the SINGLE source of truth for its own name, description and parameter
schema (JSON Schema). The engine advertises these in a per-agent manifest; the
tool extension registers whatever the manifest lists, so adding a tool touches
only Python — never the TypeScript bridge.
"""

from dataclasses import dataclass
from typing import Any, ClassVar, Protocol, runtime_checkable

from pydantic_settings import BaseSettings

from crucible.ports.chat.admin import ChatAdmin
from crucible.ports.chat.directory import AgentDirectory
from crucible.ports.chat.files import FileService
from crucible.ports.chat.interactions import InteractionService

# Capabilities a tool may require (Tool.requires). The composition root advertises
# a tool to an agent only when its gateway/config provides every required
# capability, so a tool never runs without the dependency it declares.
CAP_CHAT_ADMIN = "chat_admin"  # channel administration (Mattermost + Slack)
CAP_WIDGETS = "widgets"  # interactive widgets (buttons / selects)
CAP_FORMS = "forms"  # modal forms
CAP_EPHEMERAL = "ephemeral"  # messages visible only to one user (Mattermost + Slack)
CAP_FILES = "files"  # sending a file into the conversation


class ToolError(Exception):
    """A tool failed in an expected, user-reportable way (bad args, not found).
    Surfaced to the agent as the tool's error text, not a 500."""


@dataclass
class ToolContext:
    """Everything a tool may touch, scoped to the CALLING agent.

    ``chat_admin`` is that agent's own admin client, so any channel/invite action
    is attributed to the agent that invoked the tool. It is None on gateways
    without channel administration (e.g. Slack) — a tool that needs it must say so.
    ``settings`` is the invoked tool's OWN config object (or None) — the server
    injects it generically by tool name, so a per-tool setting never leaks into
    ToolServer/ToolContext."""

    agent_name: str
    directory: AgentDirectory
    chat_admin: ChatAdmin | None = None
    settings: Any = None
    # Interactivity: the runtime session this call runs inside (opaque; forwarded to
    # the service to resolve where to post) and the service that runs the widget/
    # form round-trip.
    runtime_session_id: str = ""
    interaction_svc: InteractionService | None = None
    # Sending a file into the conversation this call runs in (None = the
    # deployment has attachments turned off).
    file_svc: FileService | None = None
    # The conversation this call runs inside, resolved from runtime_session_id by
    # the server (plain strings — the tool layer stays free of store types).
    # channel_id: where the turn happened; user_id: who last triggered it. Empty
    # when the server can't resolve them (e.g. no session yet).
    channel_id: str = ""
    user_id: str = ""

    def require_chat_admin(self) -> ChatAdmin:
        """The agent's channel-admin client, or a ToolError if its gateway has none
        (declare CAP_CHAT_ADMIN so this can't happen for an advertised tool)."""
        if self.chat_admin is None:
            raise ToolError("channel administration is not available on this gateway")
        return self.chat_admin

    def require_files(self) -> FileService:
        """The file-sending service, or a ToolError if this deployment has files
        turned off (declare CAP_FILES so this can't happen for an advertised
        tool)."""
        if self.file_svc is None:
            raise ToolError("sending files is turned off in this deployment")
        return self.file_svc

    def require_interactions(self) -> InteractionService:
        """The widget/form service, or a ToolError if interactivity is off (declare
        CAP_WIDGETS/CAP_FORMS so this can't happen for an advertised tool)."""
        if self.interaction_svc is None:
            raise ToolError("interactive widgets/forms are not available in this context")
        return self.interaction_svc


@runtime_checkable
class Tool(Protocol):
    name: ClassVar[str]
    description: ClassVar[str]
    parameters: ClassVar[dict[str, Any]]  # JSON Schema for the tool's arguments
    # Optional: a settings model (env-bound) this tool needs. The registry loads
    # it generically at wiring time and injects the instance as ctx.settings, so
    # a new configured tool never touches app.py. None = no config.
    settings_cls: ClassVar[type[BaseSettings] | None] = None
    # When True, this tool must not run until the user has confirmed it — an
    # enforced "approve before this action" the agent can't skip. Surfaced via the
    # manifest so marking a tool needs no app.py edit; the runtime is responsible
    # for gating the call on that confirmation.
    requires_confirmation: ClassVar[bool] = False
    # Capabilities this tool needs from the agent's gateway/config (CAP_*). The
    # composition root only advertises the tool to agents that provide them all, so
    # execute() can assume they're present (ctx.require_* enforces it defensively).
    requires: ClassVar[frozenset[str]] = frozenset()

    async def execute(self, ctx: ToolContext, args: dict[str, Any]) -> Any:
        """Run the tool; return a JSON-serializable result. Raise ToolError for
        expected failures."""
        ...
