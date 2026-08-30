"""Neutral Form -> Mattermost interactive-dialog payload.

The ONE place that knows what Mattermost calls each neutral field type. Pure (no
driver), so it is unit-tested offline.

Server floors for the elements used here: ``radio`` 5.16, ``multiselect`` 11.0,
``date``/``datetime`` 11.1. Two neutral types have no native control:
``label`` (no static-text element exists — it is folded into the dialog's
markdown ``introduction_text``) and ``time`` (no time picker — a text field
hinting HH:MM).
"""

import logging
from typing import Any

from crucible.ports.chat.types import (
    CHANNEL_FIELD_TYPES,
    MULTI_FIELD_TYPES,
    STATIC_FIELD_TYPES,
    USER_FIELD_TYPES,
    Form,
    FormField,
)

logger = logging.getLogger(__name__)

# The free-text family: neutral type -> MM text subtype (the subtype only tunes
# the keyboard/validation; the value always comes back as a string).
_TEXT_SUBTYPES = {
    "text": "text",
    "textarea": "text",
    "number": "number",
    "email": "email",
    "url": "url",
    "tel": "tel",
    "password": "password",
}
_TIME_HINT = "HH:MM"
# Mattermost's own limits — exceeding them makes the dialog API reject the whole
# payload, so truncate rather than let one long label kill the form.
_DISPLAY_NAME_MAX = 24
_HELP_TEXT_MAX = 150
_PLACEHOLDER_MAX = 150


def _fit(text: str, limit: int, what: str) -> str:
    """Cut to what the platform accepts, and SAY SO. The cut itself has to stay:
    an over-long label makes the dialog API reject the whole payload, so the
    choice is a shortened label or no form at all. But a label silently arriving
    as "Agents that may ask (com" reads as a typo rather than as a limit, and
    the author is the only one who can fix it — so they get told."""
    if len(text) <= limit:
        return text
    logger.warning(
        "Mattermost caps a dialog %s at %d characters; %r was cut. Shorten it "
        "and put the rest in help_text.", what, limit, text,
    )
    return text[:limit]


def build_dialog(form: Form, *, state: str, callback_id: str = "form") -> dict[str, Any]:
    """The ``dialog`` object for ``open_interactive_dialog``."""
    dialog: dict[str, Any] = {
        "callback_id": callback_id,
        "title": _fit(form.title, _DISPLAY_NAME_MAX, "title"),
        "submit_label": form.submit_label,
        "state": state,
        "elements": [_element(f) for f in form.fields if f.type not in STATIC_FIELD_TYPES],
    }
    intro = introduction_text(form)
    if intro:
        dialog["introduction_text"] = intro
    return dialog


def introduction_text(form: Form) -> str:
    """The markdown block above the elements: the form's intro plus every
    ``label`` field, in the order the agent declared them (a label between two
    inputs can't be placed inline, but its text must not be lost)."""
    parts = [form.intro.strip()] if form.intro.strip() else []
    parts += [
        f.label.strip()
        for f in form.fields
        if f.type in STATIC_FIELD_TYPES and f.label.strip()
    ]
    return "\n\n".join(parts)


def _element(field: FormField) -> dict[str, Any]:
    el: dict[str, Any] = {
        "display_name": _fit(field.label, _DISPLAY_NAME_MAX, "label"),
        "name": field.name,
        "optional": field.optional,
    }
    if field.help_text:
        el["help_text"] = _fit(field.help_text, _HELP_TEXT_MAX, "help text")
    if field.value:
        # What the control starts out holding. MM takes it for text and select
        # alike; for a select the string has to be one of the option values.
        el["default"] = field.value
    placeholder = field.placeholder
    if field.type in _TEXT_SUBTYPES:
        el["type"] = "textarea" if field.type == "textarea" else "text"
        el["subtype"] = _TEXT_SUBTYPES[field.type]
    elif field.type == "time":
        el["type"] = "text"
        el["subtype"] = "text"
        placeholder = placeholder or _TIME_HINT
    elif field.type in ("date", "datetime"):
        el["type"] = field.type
    elif field.type == "bool":
        el["type"] = "bool"
        # MM renders the checkbox's own caption from placeholder, not display_name.
        placeholder = placeholder or field.label
    elif field.type == "radio":
        el["type"] = "radio"
        el["options"] = _options(field)
    else:  # select / multiselect / user(s) / channel(s)
        el["type"] = "select"
        if field.type in USER_FIELD_TYPES:
            el["data_source"] = "users"
        elif field.type in CHANNEL_FIELD_TYPES:
            el["data_source"] = "channels"
        else:
            el["options"] = _options(field)
        if field.type in MULTI_FIELD_TYPES:
            el["multiselect"] = True
    if placeholder:
        el["placeholder"] = placeholder[:_PLACEHOLDER_MAX]
    return el


def _options(field: FormField) -> list[dict[str, str]]:
    return [{"text": o, "value": o} for o in field.options]
