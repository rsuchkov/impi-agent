"""crucible's built-in generic typed tools: the ones that ask with real controls
— buttons, a menu, a modal form, an engine panel. Each declares `speaks_to_user`,
so what it posts IS the agent's reply and the turn may end on the call; the note
telling the model that is generated from the flag, never written out here. This
is content, not framework — it lives beside the tools package (which
holds only base/registry/server). Each tool is marked ``@tool``; the composition
root names this module to ``build_registry`` to offer them, exactly like it
names its own tool modules. Tools depend only on ports (the interaction service)."""

from collections.abc import Mapping
from typing import Any, ClassVar

from crucible.ports.chat.files import FileError
from crucible.ports.chat.interactions import ASK_CHANNELS, ASK_SELECT, ASK_USERS
from crucible.ports.chat.types import (
    FIELD_TYPES,
    STATIC_FIELD_TYPES,
    CallPreview,
    CannotRun,
    Form,
    FormField,
    PreviewRow,
)
from crucible.ports.delegation import DelegationError, FilePreview
from crucible.tools.base import (
    CAP_DELEGATION,
    CAP_EPHEMERAL,
    CAP_FILES,
    CAP_FORMS,
    CAP_WIDGETS,
    Tool,
    ToolContext,
    ToolError,
)
from crucible.tools.registry import tool

# Field types that need their own list of choices, and those that must NOT carry
# one (the platform supplies the choices, or there are none).
_CHOICE_TYPES = frozenset({"select", "multiselect", "radio"})
_MAX_FIELDS = 15
# Slack's own cap on button text; Mattermost has no hard one but wraps badly.
_BUTTON_LABEL_MAX = 75
# The tool's own `source` argument (part of its public schema) -> how the
# interaction service renders it.
_STYLES = {"options": ASK_SELECT, "users": ASK_USERS, "channels": ASK_CHANNELS}
_SELECT_SOURCES = tuple(_STYLES)


def _file_heading(preview: FilePreview) -> str:
    """The line over a file's content on the card: its name and size, and that
    it was cut when it was."""
    cut = f", cut at {len(preview.text or '')} chars" if preview.truncated else ""
    return f"── {preview.name} ({preview.size} bytes{cut})"


def _require_str(args: dict[str, Any], key: str) -> str:
    value = args.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ToolError(f"missing required string argument {key!r}")
    return value.strip()


@tool
class AskUserButtons(Tool):
    name: ClassVar[str] = "ask_user_buttons"
    destructive: ClassVar[bool] = False  # asks, reads or shows; changes nothing
    requires: ClassVar[frozenset[str]] = frozenset({CAP_WIDGETS})
    speaks_to_user: ClassVar[bool] = True
    description: ClassVar[str] = (
        "Ask the user a question in THIS conversation with clickable buttons. The "
        "click arrives later as a new message with the chosen option. Use for "
        "quick choices/confirmations instead of asking in free text."
    )
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "prompt": {"type": "string", "description": "The question shown above the buttons"},
            "options": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Button labels (2-5); the clicked one comes back as the reply",
            },
        },
        "required": ["prompt", "options"],
    }

    async def execute(self, ctx: ToolContext, args: dict[str, Any]) -> Any:
        prompt = _require_str(args, "prompt")
        options = args.get("options")
        if not isinstance(options, list) or not (2 <= len(options) <= 5):
            raise ToolError("options must be a list of 2 to 5 button labels")
        labels = [str(o).strip() for o in options if str(o).strip()]
        posted = await ctx.require_interactions().ask(ctx.agent_name, ctx.runtime_session_id, prompt, labels)
        if not posted:
            raise ToolError("could not post the buttons (conversation not resolved)")
        return {"status": "posted", "awaiting_click": True}


@tool
class AskUserSelect(Tool):
    name: ClassVar[str] = "ask_user_select"
    destructive: ClassVar[bool] = False  # asks, reads or shows; changes nothing
    requires: ClassVar[frozenset[str]] = frozenset({CAP_WIDGETS})
    speaks_to_user: ClassVar[bool] = True
    description: ClassVar[str] = (
        "Ask the user a question in THIS conversation with a dropdown menu. Like "
        "ask_user_buttons but for longer option lists (up to 20). Set `source` to "
        "'users' or 'channels' to let them pick a person or a channel from the "
        "workspace instead of your own options — the answer comes back as a name "
        "with its id. The pick arrives later as a new message."
    )
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "prompt": {"type": "string", "description": "The question shown above the menu"},
            "options": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Dropdown options (2-20) when source is 'options'; "
                "the picked one comes back as the reply",
            },
            "source": {
                "type": "string",
                "enum": list(_SELECT_SOURCES),
                "description": "Where the choices come from: your own 'options' "
                "(default), the workspace's 'users' or its 'channels'",
            },
        },
        "required": ["prompt"],
    }

    async def execute(self, ctx: ToolContext, args: dict[str, Any]) -> Any:
        prompt = _require_str(args, "prompt")
        source = str(args.get("source") or "options").strip()
        if source not in _SELECT_SOURCES:
            raise ToolError(f"unknown source {source!r} (use {', '.join(_SELECT_SOURCES)})")
        options = args.get("options")
        labels: list[str] = []
        if source == "options":
            if not isinstance(options, list) or not (2 <= len(options) <= 20):
                raise ToolError("options must be a list of 2 to 20 dropdown options")
            labels = [str(o).strip() for o in options if str(o).strip()]
        elif options:
            raise ToolError(f"source={source!r} takes no options — the workspace supplies them")
        posted = await ctx.require_interactions().ask(
            ctx.agent_name, ctx.runtime_session_id, prompt, labels, style=_STYLES[source],
        )
        if not posted:
            raise ToolError("could not post the menu (conversation not resolved)")
        return {"status": "posted", "awaiting_click": True}


@tool
class OpenForm(Tool):
    name: ClassVar[str] = "open_form"
    destructive: ClassVar[bool] = False  # asks, reads or shows; changes nothing
    requires: ClassVar[frozenset[str]] = frozenset({CAP_FORMS})
    speaks_to_user: ClassVar[bool] = True
    description: ClassVar[str] = (
        "Collect several fields from the user in ONE modal form. Posts a button "
        "(label it with `open_label`); the user clicks it, fills the modal, and the "
        "submitted values come back as a message. Use for structured input (a few "
        "related fields at once) rather than asking field-by-field. `intro` is the "
        "message the user reads in the conversation — say there what you would "
        "have said in a reply; `title` only heads the modal, which nobody sees "
        "until they click. Field types: "
        "text, textarea, number, "
        "email, url, tel; select, multiselect, radio (these need options); bool; "
        "user, users, channel, channels (pick from the workspace — the answer is a "
        "name with its id); date, datetime, time; label (static text, no input)."
    )
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "title": {"type": "string", "description": "The modal's title"},
            "intro": {"type": "string", "description": "Shown next to the button that opens the form"},
            "open_label": {
                "type": "string",
                "description": "Text on that button — word it for the task "
                "('Report a bug', 'Book a slot'). Omit for the default 'Fill in…'.",
            },
            "fields": {
                "type": "array",
                "description": f"1-{_MAX_FIELDS} fields to collect, in order",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "description": "Key the value returns under"},
                        "label": {
                            "type": "string",
                            "description": "Shown to the user (for type 'label': the text itself)",
                        },
                        "type": {"type": "string", "enum": list(FIELD_TYPES)},
                        "options": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Choices for select / multiselect / radio",
                        },
                        "optional": {"type": "boolean"},
                        "placeholder": {"type": "string"},
                        "help_text": {
                            "type": "string",
                            "description": "Hint shown under the field",
                        },
                    },
                    "required": ["name", "label"],
                },
            },
        },
        "required": ["title", "fields"],
    }

    async def execute(self, ctx: ToolContext, args: dict[str, Any]) -> Any:
        title = _require_str(args, "title")
        raw = args.get("fields")
        if not isinstance(raw, list) or not (1 <= len(raw) <= _MAX_FIELDS):
            raise ToolError(f"fields must be a list of 1 to {_MAX_FIELDS} field specs")
        fields: list[FormField] = []
        for rf in raw:
            if not isinstance(rf, dict):
                raise ToolError("each field must be an object")
            name = str(rf.get("name", "")).strip()
            label = str(rf.get("label", "")).strip()
            ftype = (str(rf.get("type", "text")).strip() or "text")
            if not name or not label:
                raise ToolError("each field needs a name and a label")
            if ftype not in FIELD_TYPES:
                raise ToolError(f"unknown field type {ftype!r} (use {', '.join(FIELD_TYPES)})")
            options = tuple(str(o).strip() for o in (rf.get("options") or []) if str(o).strip())
            if ftype in _CHOICE_TYPES and len(options) < 2:
                raise ToolError(f"{ftype} field {name!r} needs at least 2 options")
            if options and ftype not in _CHOICE_TYPES:
                raise ToolError(
                    f"field {name!r} of type {ftype!r} takes no options "
                    f"(only {', '.join(sorted(_CHOICE_TYPES))} do)"
                )
            fields.append(
                FormField(
                    name=name, label=label, type=ftype, options=options,
                    optional=bool(rf.get("optional", False)),
                    placeholder=str(rf.get("placeholder", "")),
                    help_text=str(rf.get("help_text", "")),
                )
            )
        if all(f.type in STATIC_FIELD_TYPES for f in fields):
            raise ToolError("a form needs at least one field that collects a value")
        open_label = str(args.get("open_label", "")).strip()
        if len(open_label) > _BUTTON_LABEL_MAX:
            raise ToolError(
                f"open_label must be at most {_BUTTON_LABEL_MAX} characters "
                f"(platforms truncate longer button text)"
            )
        form = Form(
            title=title, intro=str(args.get("intro", "")), fields=tuple(fields),
            open_label=open_label,
        )
        posted = await ctx.require_interactions().open_form(ctx.agent_name, ctx.runtime_session_id, form)
        if not posted:
            raise ToolError("could not post the form (conversation not resolved)")
        return {"status": "posted", "awaiting_submit": True}


@tool
class SendEphemeral(Tool):
    name: ClassVar[str] = "send_ephemeral"
    destructive: ClassVar[bool] = False  # asks, reads or shows; changes nothing
    posts_to_user: ClassVar[bool] = True  # the file/message reaches a person; the turn may end on it
    requires: ClassVar[frozenset[str]] = frozenset({CAP_EPHEMERAL})
    description: ClassVar[str] = (
        "Post a message in THIS conversation visible ONLY to one user (an "
        "ephemeral message — others in the channel don't see it, and it isn't "
        "stored in the conversation history). Defaults to the user who triggered "
        "this turn; pass `target` to send it to a specific @username instead. Use "
        "for a private hint or a heads-up you don't want to broadcast."
    )
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "message": {"type": "string", "description": "The message text (Markdown)"},
            "target": {
                "type": "string",
                "description": "Who sees it: an @username; omit to send to the "
                "user who triggered this turn",
            },
        },
        "required": ["message"],
    }

    async def execute(self, ctx: ToolContext, args: dict[str, Any]) -> Any:
        message = _require_str(args, "message")
        admin = ctx.require_chat_admin()
        if not ctx.channel_id:
            raise ToolError("no conversation context for an ephemeral message")
        target = str(args.get("target") or "").strip()
        if target:
            user_id = await admin.resolve_username(target)
            if not user_id:
                raise ToolError(f"could not resolve user {target!r}")
        elif ctx.user_id:
            user_id = ctx.user_id
        else:
            raise ToolError("no target user in this conversation (pass `target`)")
        try:
            await admin.post_ephemeral(ctx.channel_id, user_id, message)
        except Exception as exc:
            # Platforms gate ephemeral posts (Mattermost needs the
            # create_post_ephemeral permission, which a bot lacks by default) —
            # report it rather than crash the turn.
            raise ToolError(
                f"could not post the ephemeral message (does the bot have "
                f"permission to post ephemeral messages?): {exc}"
            ) from exc
        return {"delivered": True, "ephemeral": True, "user_id": user_id}


@tool
class SendFile(Tool):
    name: ClassVar[str] = "send_file"
    posts_to_user: ClassVar[bool] = True  # the file/message reaches a person; the turn may end on it
    requires: ClassVar[frozenset[str]] = frozenset({CAP_FILES})
    description: ClassVar[str] = (
        "Send a file from disk to the person you are talking to — a picture, a "
        "document, a report you just produced. Give the ABSOLUTE path of a file "
        "you wrote in your own working directory, in your attachments directory, "
        "or under /tmp; anything else is refused. The optional caption is posted "
        "as the message the file arrives with."
    )
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Absolute path of the file to send"},
            "caption": {
                "type": "string",
                "description": "Optional message posted with the file (Markdown)",
            },
        },
        "required": ["path"],
    }

    async def execute(self, ctx: ToolContext, args: dict[str, Any]) -> Any:
        path = _require_str(args, "path")
        caption = str(args.get("caption") or "")
        try:
            sent = await ctx.require_files().send(
                ctx.agent_name, ctx.runtime_session_id, [path], text=caption
            )
        except FileError as exc:
            # The reason is the agent's to act on: a wrong path is fixable, a
            # size limit means it should send something smaller.
            raise ToolError(str(exc)) from exc
        except Exception as exc:
            raise ToolError(f"could not send the file: {exc}") from exc
        return {"sent": sent}


@tool
class Delegate(Tool):
    name: ClassVar[str] = "delegate"
    display_name: ClassVar[str] = "Hand a task to another agent"
    destructive: ClassVar[bool] = False  # starts another agent's turn; changes nothing of its own
    requires_confirmation: ClassVar[bool] = True  # what leaves this conversation is read by a person first
    grantable: ClassVar[bool] = False  # each task is read: a window would let the next one go unread
    requires: ClassVar[frozenset[str]] = frozenset({CAP_DELEGATION})
    description: ClassVar[str] = (
        "Hand a task to another agent of this deployment (see list_agents): the engine opens a turn "
        "of that agent on your task alone — it sees the task and the files you give, nothing of this "
        "conversation — and when it is done, a turn of yours with its answer, quoted as that agent's "
        "words, named by the task id this call returns. The call ends at once; do not wait for the "
        "answer in this turn. Write the task so it stands on its own, and put nothing in it the other "
        "agent must not see: a person reads exactly what goes before it goes, and the task leaves this "
        "conversation for that agent's model. Give files by absolute path, from your own working "
        "directory, your attachments directory or /tmp. delegation_status asks after a task; "
        "cancel_delegation calls one off."
    )
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "agent": {"type": "string", "description": "The other agent's name, as list_agents gives it"},
            "task": {"type": "string", "description": "The task, as a message to that agent (Markdown)"},
            "files": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Absolute paths of files to send with the task",
            },
        },
        "required": ["agent", "task"],
    }

    def caption(self, args: Mapping[str, Any]) -> str | None:
        other = args.get("agent")
        return f"Handing a task to {other}" if isinstance(other, str) and other else "Handing a task on"

    async def describe(self, ctx: ToolContext, args: dict[str, Any]) -> CallPreview | CannotRun | None:
        """The card: whom the task goes to and what model answers there, the
        task as written, the files by their paths and — text — as written,
        a file that is not text by name and size. The person reads what leaves."""
        other_name = str(args.get("agent") or "")
        other = next((a for a in ctx.directory.list_agents() if a.name == other_name), None)
        if other is None:
            return CannotRun(f"no agent named {other_name!r} runs here; list_agents says who does")
        if other.name == ctx.agent_name:
            return CannotRun("that is you; hand the task to another agent")
        rows = [
            PreviewRow("Agent", f"{other.name} — {other.description}" if other.description else other.name),
            PreviewRow("Model", other.model or "not said"),
            PreviewRow("Task", str(args.get("task") or ""), passage=True),
        ]
        block, language = "", ""
        files = args.get("files")
        if isinstance(files, list) and files:
            # The paths as given: where a file comes from is part of what
            # leaves, and a name alone would hide another conversation's file.
            rows.append(PreviewRow("Files", ", ".join(str(f) for f in files)))
            try:
                previews = await ctx.require_delegation().preview_files(ctx.agent_name, [str(f) for f in files])
            except DelegationError as exc:
                return CannotRun(str(exc))
            texts = [p for p in previews if p.text is not None]
            binaries = [p for p in previews if p.text is None]
            if binaries:
                rows.append(PreviewRow("Not text", ", ".join(f"{p.name} ({p.size} bytes)" for p in binaries)))
            if texts:
                block = "\n\n".join(f"{_file_heading(p)}\n{p.text}" for p in texts)
                languages = {p.language for p in texts}
                language = languages.pop() if len(languages) == 1 else ""
        return CallPreview(
            f"Hand a task to {other.name}", tuple(rows),
            block_label="Files as written" if block else "", block=block, block_language=language,
        )

    async def execute(self, ctx: ToolContext, args: dict[str, Any]) -> Any:
        other = _require_str(args, "agent")
        task = _require_str(args, "task")
        files = args.get("files") or []
        if not isinstance(files, list) or not all(isinstance(f, str) for f in files):
            raise ToolError("files must be a list of absolute paths")
        try:
            task_id = await ctx.require_delegation().delegate(
                ctx.agent_name, ctx.runtime_session_id, other, task, files
            )
        except DelegationError as exc:
            raise ToolError(str(exc)) from exc
        except Exception as exc:
            raise ToolError(f"could not hand the task on: {exc}") from exc
        return {
            "task_id": task_id,
            "handed_to": other,
            "note": f"{other} works on it now; its answer arrives as a later turn of yours, naming task {task_id}",
        }


@tool
class DelegationStatus(Tool):
    name: ClassVar[str] = "delegation_status"
    display_name: ClassVar[str] = "Ask after a task handed on"
    destructive: ClassVar[bool] = False  # reads the task's record; changes nothing
    requires: ClassVar[frozenset[str]] = frozenset({CAP_DELEGATION})
    description: ClassVar[str] = (
        "Where a task you handed on with delegate stands, by its task id: running, answered, failed, "
        "timed_out or cancelled — and, once it ended, the other agent's answer and the notice its turn "
        "ended with. The answer also arrives as a turn of yours; ask here when it has not, or to "
        "see it again."
    )
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {"task_id": {"type": "string", "description": "The id delegate returned"}},
        "required": ["task_id"],
    }

    def caption(self, args: Mapping[str, Any]) -> str | None:
        task_id = args.get("task_id")
        return f"Asking after task {task_id}" if isinstance(task_id, str) and task_id else None

    async def execute(self, ctx: ToolContext, args: dict[str, Any]) -> Any:
        task_id = _require_str(args, "task_id")
        try:
            view = await ctx.require_delegation().status(ctx.agent_name, ctx.runtime_session_id, task_id)
        except DelegationError as exc:
            raise ToolError(str(exc)) from exc
        return {
            "task_id": view.task_id, "agent": view.to, "status": view.status,
            "started_at": view.started_at,
            **({"finished_at": view.finished_at} if view.finished_at else {}),
            **({"answer": view.answer} if view.answer else {}),
            **({"notice": view.notice} if view.notice else {}),
        }


@tool
class CancelDelegation(Tool):
    name: ClassVar[str] = "cancel_delegation"
    display_name: ClassVar[str] = "Call a task handed on off"
    destructive: ClassVar[bool] = False  # ends a turn of the engine's own making; nothing outside changes
    requires: ClassVar[frozenset[str]] = frozenset({CAP_DELEGATION})
    description: ClassVar[str] = (
        "Call off a task you handed on with delegate, by its task id: the other agent's turn is "
        "stopped and no answer comes. Says whether it was still running."
    )
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {"task_id": {"type": "string", "description": "The id delegate returned"}},
        "required": ["task_id"],
    }

    def caption(self, args: Mapping[str, Any]) -> str | None:
        task_id = args.get("task_id")
        return f"Calling task {task_id} off" if isinstance(task_id, str) and task_id else None

    async def execute(self, ctx: ToolContext, args: dict[str, Any]) -> Any:
        task_id = _require_str(args, "task_id")
        try:
            cancelled = await ctx.require_delegation().cancel(ctx.agent_name, ctx.runtime_session_id, task_id)
        except DelegationError as exc:
            raise ToolError(str(exc)) from exc
        return {
            "task_id": task_id,
            "cancelled": cancelled,
            "note": "its turn is stopped; no answer will come" if cancelled else "it had already ended",
        }


@tool
class OpenScreen(Tool):
    name: ClassVar[str] = "open_screen"
    destructive: ClassVar[bool] = False  # asks, reads or shows; changes nothing
    requires: ClassVar[frozenset[str]] = frozenset({CAP_WIDGETS})
    speaks_to_user: ClassVar[bool] = True
    description: ClassVar[str] = (
        "Post one of the engine's own interactive panels into THIS conversation "
        "— the same one its slash command opens. Use it when the user asks to "
        "see or manage what the panel is for, instead of describing it yourself: "
        "the engine renders the panel and answers every click on it, so what the "
        "user sees is always the live state, and nothing is done until they click."
    )
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "screen": {
                "type": "string",
                "description": (
                    "Which panel. The names are this engine's own command words "
                    "(commonly 'skills' and 'tasks'); asking for an unknown one "
                    "answers with the list."
                ),
            },
        },
        "required": ["screen"],
    }

    async def execute(self, ctx: ToolContext, args: dict[str, Any]) -> Any:
        name = _require_str(args, "screen")
        interactions = ctx.require_interactions()
        available = interactions.screen_names()
        opened = await interactions.open_screen(
            ctx.agent_name, ctx.runtime_session_id, name, user_id=ctx.user_id
        )
        if not opened:
            if name.lstrip("/").strip().lower() not in available:
                raise ToolError(
                    f"no panel called {name!r}. This engine has: "
                    + (", ".join(available) or "none")
                )
            raise ToolError("could not post the panel (conversation not resolved)")
        return {"opened": name}
