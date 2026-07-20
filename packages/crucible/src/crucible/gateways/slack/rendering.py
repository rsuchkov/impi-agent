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
from typing import Any

from crucible.ports.chat.types import Action, Form

# Action ids all share this prefix; the gateway binds one handler by regex on it.
WIDGET_ACTION_PREFIX = "cruxw"
# One constant callback id for every engine modal; the gateway binds app.view on it.
FORM_CALLBACK = "crux_form"
_TOKEN_BLOCK_PREFIX = "tok:"
# Slack hard limits.
_TITLE_MAX = 24
_LABEL_MAX = 75


def build_action_blocks(text: str, actions: list[Action]) -> list[dict[str, Any]]:
    """A section with ``text`` above an actions block of buttons / a select."""
    elements = [_element(a, i) for i, a in enumerate(actions)]
    return [
        {"type": "section", "text": {"type": "mrkdwn", "text": text or " "}},
        {"type": "actions", "elements": elements},
    ]


def _element(action: Action, index: int) -> dict[str, Any]:
    action_id = f"{WIDGET_ACTION_PREFIX}{index}"
    if action.kind == "select":
        return {
            "type": "static_select",
            "action_id": action_id,
            "block_id": f"{_TOKEN_BLOCK_PREFIX}{action.context.get('token', '')}",
            "placeholder": {"type": "plain_text", "text": action.label[:_LABEL_MAX]},
            "options": [
                {"text": {"type": "plain_text", "text": o[:_LABEL_MAX]}, "value": o}
                for o in action.options
            ],
        }
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
    """A block_actions element -> (token, form_token, value). A select yields the
    token from its block_id and the pick from selected_option; a button yields all
    three from its value JSON."""
    if action.get("type") == "static_select" or "selected_option" in action:
        token = str(action.get("block_id", "")).removeprefix(_TOKEN_BLOCK_PREFIX)
        picked = (action.get("selected_option") or {}).get("value", "")
        return token, "", str(picked)
    meta = _load_json(action.get("value"))
    return str(meta.get("token", "")), str(meta.get("form", "")), str(meta.get("value", ""))


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
        "blocks": [_input_block(f) for f in form.fields],
    }


def _input_block(field) -> dict[str, Any]:
    if field.type == "select":
        element: dict[str, Any] = {
            "type": "static_select",
            "action_id": field.name,
            "options": [
                {"text": {"type": "plain_text", "text": o[:_LABEL_MAX]}, "value": o}
                for o in field.options
            ],
        }
    elif field.type == "bool":
        element = {
            "type": "static_select",
            "action_id": field.name,
            "options": [
                {"text": {"type": "plain_text", "text": "Yes"}, "value": "yes"},
                {"text": {"type": "plain_text", "text": "No"}, "value": "no"},
            ],
        }
    else:  # text / textarea
        element = {"type": "plain_text_input", "action_id": field.name}
        if field.type == "textarea":
            element["multiline"] = True
        if field.placeholder:
            element["placeholder"] = {"type": "plain_text", "text": field.placeholder}
    return {
        "type": "input",
        "block_id": field.name,
        "optional": field.optional,
        "label": {"type": "plain_text", "text": field.label[:_LABEL_MAX]},
        "element": element,
    }


def extract_submission(view_state: dict[str, Any]) -> dict[str, str]:
    """Flatten Slack's ``view.state.values[block_id][action_id]`` (block_id ==
    action_id here) into ``{field_name: value}``."""
    out: dict[str, str] = {}
    for block_id, actions in (view_state.get("values") or {}).items():
        element = actions.get(block_id) or next(iter(actions.values()), {})
        if element.get("value") is not None:
            out[block_id] = element["value"]
        elif element.get("selected_option"):
            out[block_id] = element["selected_option"].get("value", "")
        else:
            out[block_id] = ""
    return out


def _load_json(value: Any) -> dict[str, Any]:
    if isinstance(value, str) and value:
        try:
            data = json.loads(value)
            return data if isinstance(data, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}
