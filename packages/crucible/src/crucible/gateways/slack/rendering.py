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
    Card,
    Form,
    FormField,
)

# Action ids all share this prefix; the gateway binds one handler by regex on it.
WIDGET_ACTION_PREFIX = "cruxw"
# One constant callback id for every engine modal; the gateway binds app.view on it.
FORM_CALLBACK = "crux_form"
_TOKEN_BLOCK_PREFIX = "tok:"
# A menu inside an engine screen: its block_id carries the screen and its state
# (a button uses its value for the same job). Block ids cap at 255 characters,
# which is why screen state is deliberately small.
_SCREEN_BLOCK_PREFIX = "scr:"
_SCREEN_BLOCK_SEP = "|"
# A menu answering a request for authorization — the "allow for a while"
# dropdown, whether what is being authorized is a credential or a tool call.
# Same trick as a screen's: the routing rides in the block_id.
_APPROVAL_BLOCK_PREFIX = "apr:"
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
    # Slack has no masked input. Plain text is the honest rendering: a form that
    # refused to open would be worse, and masking was never what keeps a value
    # safe — see the note in docs/creating-agents.md.
    "password": "plain_text_input",
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
    return [
        {"type": "section", "text": {"type": "mrkdwn", "text": text or " "}},
        _actions_block(actions),
    ]


def build_card_blocks(cards: list[Card]) -> list[dict[str, Any]]:
    """Cards -> Block Kit: each card is a section with its text, followed by its
    own actions block, so a control sits under the thing it acts on. A divider
    between cards keeps a long list readable."""
    blocks: list[dict[str, Any]] = []
    index = 0
    for card in cards:
        # A text-less card is a continuation row (more controls for the card
        # above), so it gets no divider of its own.
        if blocks and card.text:
            blocks.append({"type": "divider"})
        if card.text:
            blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": card.text}})
        if card.actions:
            # action_ids must be unique across the WHOLE message, not the card.
            blocks.append(_actions_block(list(card.actions), offset=index))
            index += len(card.actions)
    return blocks


def _actions_block(actions: list[Action], *, offset: int = 0) -> dict[str, Any]:
    """One actions block, carrying what its MENU needs to round-trip.

    ``block_id`` is a property of a BLOCK — Slack rejects the whole message when
    it appears on an element ("invalid additional property: block_id"). So the
    menu's token/screen state lives here, on the block, and Slack echoes it back
    on the action it delivers. Buttons need none: they carry theirs in ``value``.

    One menu per block, which every caller satisfies today (a widget posts a
    single dropdown; a screen card pairs at most one menu with buttons). Two
    menus in one block would need a block each — they'd otherwise share this id.
    """
    block: dict[str, Any] = {
        "type": "actions",
        "elements": [_element(a, offset + i) for i, a in enumerate(actions)],
    }
    menu = next((a for a in actions if a.kind in _MENU_TYPES), None)
    if menu is not None:
        block["block_id"] = _menu_block_id(menu)
    return block


def _element(action: Action, index: int) -> dict[str, Any]:
    action_id = f"{WIDGET_ACTION_PREFIX}{index}"
    if action.kind in _MENU_TYPES:
        menu: dict[str, Any] = {
            "type": _MENU_TYPES[action.kind],
            "action_id": action_id,
            # No block_id here: it belongs to the containing block (_actions_block).
            "placeholder": {"type": "plain_text", "text": action.label[:_LABEL_MAX]},
        }
        if action.kind == ACTION_SELECT:
            menu["options"] = [_option(c.value, c.label) for c in action.options]
        return menu
    element: dict[str, Any] = {
        "type": "button",
        "text": {"type": "plain_text", "text": action.label[:_LABEL_MAX], "emoji": True},
        "action_id": f"{WIDGET_ACTION_PREFIX}{index}",
        # Slack has no free-form callback context, so everything the click must
        # carry — widget token, form token, screen routing — rides in the value.
        "value": json.dumps(
            {
                "token": action.context.get("token", ""),
                "form": action.context.get("form", ""),
                "value": action.value,
                "screen": action.context.get("screen", ""),
                "state": action.context.get("state", ""),
                "approval": action.context.get("approval", ""),
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


def _menu_block_id(action: Action) -> str:
    """Where a menu parks what its click must carry: a screen's routing, the
    approval it answers, or the widget token."""
    screen = action.context.get("screen", "")
    if screen:
        return (
            f"{_SCREEN_BLOCK_PREFIX}{screen}{_SCREEN_BLOCK_SEP}"
            f"{action.context.get('state', '')}"
        )
    approval = action.context.get("approval", "")
    if approval:
        return f"{_APPROVAL_BLOCK_PREFIX}{approval}"
    return f"{_TOKEN_BLOCK_PREFIX}{action.context.get('token', '')}"


def decode_approval(action: dict[str, Any]) -> str:
    """The token when this click answers a request for authorization, ""
    otherwise. A menu keeps it in its block_id, a button in its value."""
    meta = _load_json(action.get("value"))
    if meta.get("approval"):
        return str(meta["approval"])
    block_id = str(action.get("block_id", ""))
    if block_id.startswith(_APPROVAL_BLOCK_PREFIX):
        return block_id[len(_APPROVAL_BLOCK_PREFIX) :]
    return ""


def _split_screen_block(block_id: str) -> tuple[str, str]:
    name, _, state = block_id[len(_SCREEN_BLOCK_PREFIX):].partition(_SCREEN_BLOCK_SEP)
    return name, state


def decode_screen(action: dict[str, Any]) -> tuple[str, str]:
    """(screen name, encoded state) when the click belongs to an engine screen,
    ("", "") otherwise. A menu keeps it in its block_id, a button in its value."""
    meta = _load_json(action.get("value"))
    if meta.get("screen"):
        return str(meta["screen"]), str(meta.get("state", ""))
    block_id = str(action.get("block_id", ""))
    if block_id.startswith(_SCREEN_BLOCK_PREFIX):
        return _split_screen_block(block_id)
    return "", ""


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
    _prefill(element, field)
    return element


def _prefill(element: dict[str, Any], field: FormField) -> None:
    """What the control starts out holding. Slack spells it a different way per
    element, and an option that is not among the offered ones fails the view
    schema — so a value that names no option is dropped rather than sent."""
    if not field.value:
        return
    if field.type in ("select", "radio"):
        if field.value in field.options:
            element["initial_option"] = _option(field.value)
    elif field.type == "multiselect":
        # Symmetric with how a multi-pick comes BACK from Slack: ", "-joined.
        picked = [v.strip() for v in field.value.split(",") if v.strip() in field.options]
        if picked:
            element["initial_options"] = [_option(v) for v in picked]
    elif field.type in ("date", "datetime"):
        element["initial_date"] = field.value
    elif field.type == "time":
        element["initial_time"] = field.value
    elif field.type != "bool":
        element["initial_value"] = field.value


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
