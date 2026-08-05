"""Block Kit rendering + the widget token round-trip for Slack.

The neutral interaction machinery round-trips an opaque token (and a chosen value)
through a click. Slack has no free-form callback context like Mattermost, so we
encode it into Block Kit fields the platform echoes back:
- a button carries ``{"token","form","value"}`` JSON in its ``value``;
- a select carries the token in its ``block_id`` and the pick in
  ``selected_option.value``;
- a modal round-trips the form token in ``private_metadata``.

All functions here are pure (no Slack SDK), so they are unit-tested offline.
"""

import json
from datetime import datetime, timezone
from typing import Any

from crucible.ports.chat.types import (
    ACTION_CHANNEL_SELECT,
    ACTION_SELECT,
    ACTION_USER_SELECT,
    PICK_FIELD_BY_KIND,
    Action,
    Form,
    FormField,
)

# Action ids all share this prefix; the gateway binds one handler by regex on it.
WIDGET_ACTION_PREFIX = "cruxw"
# One constant callback id for every engine modal; the gateway binds app.view on it.
FORM_CALLBACK = "crux_form"
_TOKEN_BLOCK_PREFIX = "tok:"
# Slack hard limits.
_TITLE_MAX = 24
_LABEL_MAX = 75

# Neutral action kind -> the menu element Slack renders in an actions block.
_MENU_TYPES = {
    ACTION_SELECT: "static_select",
    ACTION_USER_SELECT: "users_select",
    ACTION_CHANNEL_SELECT: "channels_select",
}
# Neutral field type -> Block Kit input element. The families that need extra
# keys (options, multiline, ...) are handled in _input_element.
_FIELD_ELEMENTS = {
    "text": "plain_text_input",
    "textarea": "plain_text_input",
    "tel": "plain_text_input",
    "number": "number_input",
    "email": "email_text_input",
    "url": "url_text_input",
    "select": "static_select",
    "multiselect": "multi_static_select",
    "radio": "radio_buttons",
    "bool": "checkboxes",
    "user": "users_select",
    "users": "multi_users_select",
    "channel": "channels_select",
    "channels": "multi_channels_select",
    "date": "datepicker",
    "datetime": "datetimepicker",
    "time": "timepicker",
}
# Where a MENU element parks its pick. Deliberately without "value": in a click
# payload that key belongs to a button and carries our own JSON envelope.
_MENU_PICK_KEYS = ("selected_date", "selected_time", "selected_user", "selected_channel",
                   "selected_conversation")
# The same for a modal submission, where a text input's answer IS "value".
_VALUE_KEYS = ("value", *_MENU_PICK_KEYS)
_LIST_KEYS = ("selected_options", "selected_users", "selected_channels", "selected_conversations")
# The checkbox that stands in for a neutral bool: one option, ticked or not.
_BOOL_OPTION_VALUE = "yes"
# Which menu elements hand back an id rather than a value of our own making —
# derived from the table above, so the two never drift apart.
_PICK_KINDS = {
    _MENU_TYPES[kind]: field_type for kind, field_type in PICK_FIELD_BY_KIND.items()
}


def build_action_blocks(text: str, actions: list[Action]) -> list[dict[str, Any]]:
    """A section with ``text`` above an actions block of buttons / a select."""
    elements = [_element(a, i) for i, a in enumerate(actions)]
    return [
        {"type": "section", "text": {"type": "mrkdwn", "text": text or " "}},
        {"type": "actions", "elements": elements},
    ]


def _element(action: Action, index: int) -> dict[str, Any]:
    action_id = f"{WIDGET_ACTION_PREFIX}{index}"
    if action.kind in _MENU_TYPES:
        menu: dict[str, Any] = {
            "type": _MENU_TYPES[action.kind],
            "action_id": action_id,
            "block_id": f"{_TOKEN_BLOCK_PREFIX}{action.context.get('token', '')}",
            "placeholder": {"type": "plain_text", "text": action.label[:_LABEL_MAX]},
        }
        if action.kind == ACTION_SELECT:
            menu["options"] = [
                {"text": {"type": "plain_text", "text": o[:_LABEL_MAX]}, "value": o}
                for o in action.options
            ]
        return menu
    element: dict[str, Any] = {
        "type": "button",
        "text": {"type": "plain_text", "text": action.label[:_LABEL_MAX], "emoji": True},
        "action_id": f"{WIDGET_ACTION_PREFIX}{index}",
        "value": json.dumps(
            {
                "token": action.context.get("token", ""),
                "form": action.context.get("form", ""),
                "value": action.value,
            }
        ),
    }
    if action.style in ("primary", "danger"):
        element["style"] = action.style
    return element


def decode_action(action: dict[str, Any]) -> tuple[str, str, str]:
    """A block_actions element -> (token, form_token, value). Any menu yields the
    token from its block_id and the pick from its own ``selected_*`` key (an
    option's value, or the id of the picked person/channel); a button yields all
    three from its value JSON."""
    if action.get("type") in _MENU_TYPES.values() or _picked(action) is not None:
        token = str(action.get("block_id", "")).removeprefix(_TOKEN_BLOCK_PREFIX)
        return token, "", str(_picked(action) or "")
    meta = _load_json(action.get("value"))
    return str(meta.get("token", "")), str(meta.get("form", "")), str(meta.get("value", ""))


def picked_kind(action: dict[str, Any]) -> str:
    """"user" / "channel" when this element is a workspace picker, else "" — what
    the engine needs to know to resolve the returned id to a name."""
    return _PICK_KINDS.get(str(action.get("type", "")), "")


def _picked(action: dict[str, Any]) -> str | None:
    """The pick of a menu element, whatever key the platform used for it."""
    if action.get("selected_option"):
        return str((action["selected_option"] or {}).get("value", ""))
    for key in _MENU_PICK_KEYS:
        if action.get(key):
            return str(action[key])
    return None


def build_modal_view(form: Form, state: str) -> dict[str, Any]:
    """A modal ``view`` from a neutral Form. ``state`` (the form token) round-trips
    in private_metadata; each field's block_id == action_id == field.name, so the
    submitted values flatten cleanly (see extract_submission)."""
    return {
        "type": "modal",
        "callback_id": FORM_CALLBACK,
        "private_metadata": state,
        "title": {"type": "plain_text", "text": (form.title or "Form")[:_TITLE_MAX]},
        "submit": {"type": "plain_text", "text": (form.submit_label or "Submit")[:_TITLE_MAX]},
        "close": {"type": "plain_text", "text": "Cancel"},
        "blocks": [_block(f) for f in form.fields],
    }


def _block(field: FormField) -> dict[str, Any]:
    """One modal block. A ``label`` field is static text (a section block, no
    value); everything else is an input block carrying its element."""
    if field.type == "label":
        return {"type": "section", "text": {"type": "mrkdwn", "text": field.label}}
    block: dict[str, Any] = {
        "type": "input",
        "block_id": field.name,
        "optional": field.optional,
        "label": {"type": "plain_text", "text": field.label[:_LABEL_MAX]},
        "element": _input_element(field),
    }
    if field.help_text:
        block["hint"] = {"type": "plain_text", "text": field.help_text[:_LABEL_MAX * 2]}
    return block


def _input_element(field: FormField) -> dict[str, Any]:
    element: dict[str, Any] = {
        "type": _FIELD_ELEMENTS.get(field.type, "plain_text_input"),
        "action_id": field.name,
    }
    if field.type == "textarea":
        element["multiline"] = True
    elif field.type == "number":
        # Required by Slack: a number_input without it fails the view schema.
        element["is_decimal_allowed"] = True
    elif field.type in ("select", "multiselect", "radio"):
        element["options"] = [_option(o) for o in field.options]
    elif field.type == "bool":
        # Slack has no boolean input: one checkbox, ticked or not. Its caption is
        # the placeholder (the field's own label is already the block label).
        element["options"] = [_option(_BOOL_OPTION_VALUE, field.placeholder or "Yes")]
    if field.placeholder and field.type not in ("bool", "radio", "date", "datetime", "time"):
        element["placeholder"] = {"type": "plain_text", "text": field.placeholder[:_LABEL_MAX * 2]}
    return element


def _option(value: str, text: str = "") -> dict[str, Any]:
    return {"text": {"type": "plain_text", "text": (text or value)[:_LABEL_MAX]}, "value": value}


def extract_submission(view_state: dict[str, Any]) -> dict[str, str]:
    """Flatten Slack's ``view.state.values[block_id][action_id]`` (block_id ==
    action_id here) into ``{field_name: value}`` — one string per field, whichever
    ``selected_*`` key the element used, multi-picks joined."""
    out: dict[str, str] = {}
    for block_id, actions in (view_state.get("values") or {}).items():
        element = actions.get(block_id) or next(iter(actions.values()), {})
        out[block_id] = _submitted(element)
    return out


def _submitted(element: dict[str, Any]) -> str:
    if element.get("selected_option"):
        return str((element["selected_option"] or {}).get("value", ""))
    for key in _LIST_KEYS:
        picked = element.get(key)
        if picked:
            # An options list carries dicts; a users/channels list carries ids.
            return ", ".join(
                str(p.get("value", "")) if isinstance(p, dict) else str(p) for p in picked
            )
    for key in _VALUE_KEYS:
        if element.get(key) is not None:
            return str(element[key])
    if element.get("selected_date_time") is not None:  # datetimepicker: unix seconds
        return _iso(element["selected_date_time"])
    return ""


def _iso(epoch: Any) -> str:
    try:
        return datetime.fromtimestamp(int(epoch), tz=timezone.utc).isoformat(timespec="minutes")
    except (TypeError, ValueError):
        return str(epoch)


def _load_json(value: Any) -> dict[str, Any]:
    if isinstance(value, str) and value:
        try:
            data = json.loads(value)
            return data if isinstance(data, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}
