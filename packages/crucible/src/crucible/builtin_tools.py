"""crucible's built-in generic typed tools: fire-and-forget widgets and modal
forms. This is content, not framework — it lives beside the tools package (which
holds only base/registry/server). Importing it runs the @tool decorators, so the
composition root imports it to include these tools, exactly like an app imports
its own tool modules. Tools depend only on ports (widgets, forms)."""

from typing import Any, ClassVar

from crucible.ports.chat.types import Form, FormField
from crucible.tools.base import CAP_FORMS, CAP_WIDGETS, Tool, ToolContext, ToolError
from crucible.tools.registry import tool

_FIELD_TYPES = ("text", "textarea", "select", "bool")


def _require_str(args: dict[str, Any], key: str) -> str:
    value = args.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ToolError(f"missing required string argument {key!r}")
    return value.strip()


@tool
class AskUserButtons(Tool):
    name: ClassVar[str] = "ask_user_buttons"
    requires: ClassVar[frozenset[str]] = frozenset({CAP_WIDGETS})
    description: ClassVar[str] = (
        "Ask the user a question in THIS conversation with clickable buttons. "
        "Fire-and-forget: the buttons are posted and your turn ends; the click "
        "arrives later as a new message with the chosen option. Use for quick "
        "choices/confirmations instead of asking in free text."
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
        posted = await ctx.require_widgets().ask(ctx.agent_name, ctx.runtime_session_id, prompt, labels)
        if not posted:
            raise ToolError("could not post the buttons (conversation not resolved)")
        return {"status": "posted", "awaiting_click": True}


@tool
class AskUserSelect(Tool):
    name: ClassVar[str] = "ask_user_select"
    requires: ClassVar[frozenset[str]] = frozenset({CAP_WIDGETS})
    description: ClassVar[str] = (
        "Ask the user a question in THIS conversation with a dropdown menu. Like "
        "ask_user_buttons but for longer option lists (up to 20). Fire-and-forget: "
        "the menu is posted and your turn ends; the pick arrives later as a new "
        "message with the chosen option."
    )
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "prompt": {"type": "string", "description": "The question shown above the menu"},
            "options": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Dropdown options (2-20); the picked one comes back as the reply",
            },
        },
        "required": ["prompt", "options"],
    }

    async def execute(self, ctx: ToolContext, args: dict[str, Any]) -> Any:
        prompt = _require_str(args, "prompt")
        options = args.get("options")
        if not isinstance(options, list) or not (2 <= len(options) <= 20):
            raise ToolError("options must be a list of 2 to 20 dropdown options")
        labels = [str(o).strip() for o in options if str(o).strip()]
        posted = await ctx.require_widgets().ask(
            ctx.agent_name, ctx.runtime_session_id, prompt, labels, style="select"
        )
        if not posted:
            raise ToolError("could not post the menu (conversation not resolved)")
        return {"status": "posted", "awaiting_click": True}


@tool
class OpenForm(Tool):
    name: ClassVar[str] = "open_form"
    requires: ClassVar[frozenset[str]] = frozenset({CAP_FORMS})
    description: ClassVar[str] = (
        "Collect several fields from the user in ONE modal form. Posts a 'Fill in' "
        "button; the user clicks it, fills the modal, and the submitted values come "
        "back as a message. Use for structured input (a few related fields at once) "
        "rather than asking field-by-field. Field types: text, textarea, select "
        "(needs options), bool."
    )
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "title": {"type": "string", "description": "The modal's title"},
            "intro": {"type": "string", "description": "Shown next to the 'Fill in' button"},
            "fields": {
                "type": "array",
                "description": "1-10 fields to collect",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "description": "Key the value returns under"},
                        "label": {"type": "string", "description": "Shown to the user"},
                        "type": {"type": "string", "enum": list(_FIELD_TYPES)},
                        "options": {"type": "array", "items": {"type": "string"}},
                        "optional": {"type": "boolean"},
                        "placeholder": {"type": "string"},
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
        if not isinstance(raw, list) or not (1 <= len(raw) <= 10):
            raise ToolError("fields must be a list of 1 to 10 field specs")
        fields: list[FormField] = []
        for rf in raw:
            if not isinstance(rf, dict):
                raise ToolError("each field must be an object")
            name = str(rf.get("name", "")).strip()
            label = str(rf.get("label", "")).strip()
            ftype = (str(rf.get("type", "text")).strip() or "text")
            if not name or not label:
                raise ToolError("each field needs a name and a label")
            if ftype not in _FIELD_TYPES:
                raise ToolError(f"unknown field type {ftype!r} (use {', '.join(_FIELD_TYPES)})")
            options = tuple(str(o).strip() for o in (rf.get("options") or []) if str(o).strip())
            if ftype == "select" and len(options) < 2:
                raise ToolError(f"select field {name!r} needs at least 2 options")
            fields.append(
                FormField(
                    name=name, label=label, type=ftype, options=options,
                    optional=bool(rf.get("optional", False)),
                    placeholder=str(rf.get("placeholder", "")),
                )
            )
        form = Form(title=title, intro=str(args.get("intro", "")), fields=tuple(fields))
        posted = await ctx.require_forms().open(ctx.agent_name, ctx.runtime_session_id, form)
        if not posted:
            raise ToolError("could not post the form (conversation not resolved)")
        return {"status": "posted", "awaiting_submit": True}
