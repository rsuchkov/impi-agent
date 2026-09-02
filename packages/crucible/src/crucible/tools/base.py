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
from crucible.ports.tasks import TaskService

# Capabilities a tool may require (Tool.requires). The composition root advertises
# a tool to an agent only when its gateway/config provides every required
# capability, so a tool never runs without the dependency it declares.
CAP_CHAT_ADMIN = "chat_admin"  # channel administration (Mattermost + Slack)
CAP_WIDGETS = "widgets"  # interactive widgets (buttons / selects)
CAP_FORMS = "forms"  # modal forms
CAP_EPHEMERAL = "ephemeral"  # messages visible only to one user (Mattermost + Slack)
CAP_FILES = "files"  # sending a file into the conversation
CAP_SCHEDULER = "scheduler"  # scheduling work for later

# What a tool that speaks for itself tells the model, appended to its advertised
# description and returned beside its result. ONE wording for every such tool:
# it used to be written by hand in each description, in three different phrasings,
# and the fourth tool was simply never told — which is how an agent came to post a
# widget and then say the same thing again in a message of its own.
#
# The middle sentence is the mechanism, stated plainly rather than as a rule:
# a turn's own text is delivered when the turn ends, so a lead-in written before
# the call lands AFTER the message the call posts, describing something already
# on screen. Knowing that is what stops the model writing one.
SPEAKS_TO_USER_NOTE = (
    "This tool posts into the conversation itself, and what it posts is what the "
    "user sees from you — so put what you want to say in its own text. Your own "
    "reply is delivered only when the turn ends, which is after the message this "
    "tool posts. Your turn can end here. Write something afterwards only if it "
    "adds what the posted message does not say; never repeat or summarise it, and "
    "never answer as if the user had already replied."
)


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
    # Scheduling work for later (None = the deployment has the scheduler off).
    task_svc: TaskService | None = None
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

    def require_tasks(self) -> TaskService:
        """The scheduling service, or a ToolError if this deployment has the
        scheduler off (declare CAP_SCHEDULER so this can't happen for an
        advertised tool)."""
        if self.task_svc is None:
            raise ToolError("scheduling is turned off in this deployment")
        return self.task_svc

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
    # When True, this tool puts a message in front of the user itself, so what it
    # posts IS the agent's reply and the turn may end on the call. Declared rather
    # than described: the registry appends SPEAKS_TO_USER_NOTE to the advertised
    # description and the server returns it beside the result, so every such tool
    # says the same thing and a new one cannot be forgotten. A description that
    # spells this out by hand is a bug — it is what let the wording drift.
    #
    # It is about speaking, not about posting: a tool that hands over a file or a
    # notice still leaves the agent something worth saying, and telling it not to
    # repeat itself there would be wrong advice.
    speaks_to_user: ClassVar[bool] = False
    # Capabilities this tool needs from the agent's gateway/config (CAP_*). The
    # composition root only advertises the tool to agents that provide them all, so
    # execute() can assume they're present (ctx.require_* enforces it defensively).
    requires: ClassVar[frozenset[str]] = frozenset()

    async def execute(self, ctx: ToolContext, args: dict[str, Any]) -> Any:
        """Run the tool; return a JSON-serializable result. Raise ToolError for
        expected failures."""
        ...
