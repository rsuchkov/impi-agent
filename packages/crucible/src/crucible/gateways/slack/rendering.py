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

from crucible.approvals.card import card_layout
from crucible.containment import code_span, one_line
from crucible.gateways.slack.formatter import markdown_to_mrkdwn
from crucible.ports.chat.types import (
    ACTION_CHANNEL_SELECT,
    ACTION_SELECT,
    ACTION_USER_SELECT,
    PICK_FIELD_BY_KIND,
    Action,
    ApprovalCard,
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


# Block Kit allows ten fields in a section; a card with more continues in a
# second section rather than losing rows.
_FIELDS_PER_SECTION = 10


def _mrkdwn_text(text: str) -> str:
    """Caller text for mrkdwn: the three characters Slack reads as markup
    (`<…>` is a link or a mention, `&` an entity) become entities — inside a
    code span too, where Slack still resolves them."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _mrkdwn_code(text: str) -> str:
    """A value as inline code Slack will keep inline. mrkdwn knows one fence
    only — a single backtick — so a backtick inside the value is swapped for a
    look-alike (U+02CB) rather than wrapped in a longer fence it cannot read."""
    return _mrkdwn_text(code_span(text.replace("`", "\u02cb")))


def _deadline(expires_at: str) -> str:
    """The deadline as the reader's own clock shows it, with the ISO form as
    the fallback Slack prints when it cannot."""
    try:
        when = datetime.fromisoformat(expires_at)
    except ValueError:
        return f"Answer by {one_line(expires_at)}"
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    stamp = int(when.timestamp())
    return f"Answer by <!date^{stamp}^{{date_short_pretty}} at {{time}}|{one_line(expires_at)}>"


def build_confirmation_blocks(card: ApprovalCard, actions: list[Action]) -> list[dict[str, Any]]:
    """An ``ApprovalCard`` as native blocks, from the layout every platform
    shares (``card_layout``): the heading, the verdict once there is one, the
    facts as plain fields (a code span reads as an error on Slack), each
    passage as a quote with its lines, the verbatim block as fenced sections,
    the agent and the tool small in a context line with the deadline, then the
    answers. Values are the caller's text and go through the same containment
    as the Markdown form, plus the escaping mrkdwn needs."""
    layout = card_layout(card)
    # A header block is plain text: nothing in a title can style it, link from
    # it or mention anyone. Block Kit caps it at 150 characters.
    heading = f"{layout.icon} {layout.title}" if layout.title else f"{layout.icon} {layout.agent} {layout.headline}"
    blocks: list[dict[str, Any]] = [
        {"type": "header", "text": {"type": "plain_text", "text": _header_text(heading)}}
    ]
    if layout.verdict:
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": _mrkdwn_text(layout.verdict)}})
    fields = [
        {"type": "mrkdwn", "text": f"*{_mrkdwn_text(label)}:* {_mrkdwn_text(value)}"}
        for label, value in layout.facts
    ]
    for start in range(0, len(fields), _FIELDS_PER_SECTION):
        blocks.append({"type": "section", "fields": fields[start : start + _FIELDS_PER_SECTION]})
    for label, text in layout.passages:
        quoted = "\n".join(f"> {_mrkdwn_text(line)}" for line in text.split("\n"))
        blocks += _sections(f"*{_mrkdwn_text(label)}*", quoted)
    if layout.block:
        label = f"*{_mrkdwn_text(layout.block_label)}*" if layout.block_label else ""
        blocks += _sections(label, _mrkdwn_text(layout.block), fence="```")
    signature = [_mrkdwn_text(part) for part in layout.signature]
    if card.expires_at and actions:
        signature.append(_deadline(card.expires_at))
    if signature:
        blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": " · ".join(signature)}]})
    if actions:  # an answered card has nothing left to click
        blocks.append(_actions_block(actions))
    return blocks


# Block Kit's cap on a header's text.
_HEADER_MAX = 150


def _header_text(text: str) -> str:
    return text if len(text) <= _HEADER_MAX else text[: _HEADER_MAX - 1].rstrip() + "…"


# What one mrkdwn section may hold. A passage or a block past it goes on in
# further sections, split between lines, each fenced on its own when fenced.
_SECTION_MAX = 3000
# How many sections one passage may take: a card is one message of at most
# fifty blocks, and the rest of the card needs room too.
_SECTIONS_MAX = 4


def _sections(label: str, text: str, *, fence: str = "") -> list[dict[str, Any]]:
    """``text`` as one or more sections of at most ``_SECTION_MAX`` characters,
    split between lines; the label on the first, ``fence`` around each."""
    room = _SECTION_MAX - len(label) - 2 * len(fence) - 4
    pieces: list[str] = []
    current = ""
    for line in text.split("\n"):
        if current and len(current) + 1 + len(line) > room:
            pieces.append(current)
            current = line
        else:
            current = f"{current}\n{line}" if current else line
        while len(current) > room:  # one line longer than a section
            pieces.append(current[:room])
            current = current[room:]
    pieces.append(current)
    if len(pieces) > _SECTIONS_MAX:
        shown, dropped = pieces[:_SECTIONS_MAX], len(pieces) - _SECTIONS_MAX
        shown[-1] += f"\n… {dropped} more section(s) cut"
        pieces = shown
    blocks: list[dict[str, Any]] = []
    for index, piece in enumerate(pieces):
        body = f"{fence}\n{piece}\n{fence}" if fence else piece
        head = f"{label}\n" if index == 0 and label else ""
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": head + body}})
    return blocks


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
            # Card text is Markdown, like every other model- and engine-authored
            # string that reaches this gateway. It used to go into the mrkdwn
            # section verbatim, so `**name**` arrived with its asterisks showing
            # while `code` looked right — backticks happen to mean the same
            # thing in both, which is what made the gap easy to miss.
            blocks.append({
                "type": "section",
                "text": {"type": "mrkdwn", "text": markdown_to_mrkdwn(card.text)},
            })
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
                "id": action.id,
                "data": action.data,
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


def decode_click(action: dict[str, Any]) -> tuple[str, str]:
    """A block_actions element -> (the control's own id, the application's
    data on it); a menu carries neither."""
    if action.get("type") in _MENU_TYPES.values() or _picked(action) is not None:
        return "", ""
    meta = _load_json(action.get("value"))
    return str(meta.get("id", "")), str(meta.get("data", ""))


def chosen_label(action: dict[str, Any]) -> str:
    """What the person saw on the control they used: a button's text, a
    picked option's text; "" when the element says neither."""
    picked = action.get("selected_option")
    if isinstance(picked, dict):
        return str((picked.get("text") or {}).get("text", ""))
    text = action.get("text")
    return str(text.get("text", "")) if isinstance(text, dict) else ""


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
