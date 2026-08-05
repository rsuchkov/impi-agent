"""Slack Block Kit rendering + the widget token round-trip (pure, offline)."""

import pytest

from crucible.gateways.slack.rendering import (
    FORM_CALLBACK,
    build_action_blocks,
    build_modal_view,
    decode_action,
    extract_submission,
    picked_kind,
)
from crucible.ports.chat.types import Action, Form, FormField


def _button_element(blocks) -> dict:
    return blocks[1]["elements"][0]


def test_button_round_trips_token_and_value() -> None:
    action = Action(id="opt0", label="Yes", value="Yes", style="primary", context={"token": "T1"})
    blocks = build_action_blocks("Choose", [action])
    el = _button_element(blocks)
    assert el["type"] == "button"
    assert el["style"] == "primary"
    # what Slack echoes on click is this element with its value:
    token, form_token, value = decode_action(el)
    assert (token, form_token, value) == ("T1", "", "Yes")


def test_form_open_button_round_trips_form_token() -> None:
    action = Action(id="openform", label="📝 Fill in", context={"form": "F1"})
    el = _button_element(build_action_blocks("intro", [action]))
    token, form_token, value = decode_action(el)
    assert (token, form_token, value) == ("", "F1", "")


def test_select_round_trips_token_and_pick() -> None:
    action = Action(id="sel", label="Pick", kind="select", options=("a", "b"), context={"token": "T2"})
    el = _button_element(build_action_blocks("Choose", [action]))
    assert el["type"] == "static_select"
    assert el["block_id"] == "tok:T2"
    # simulate the block_actions payload Slack sends when "b" is picked:
    clicked = {**el, "selected_option": {"value": "b", "text": {"type": "plain_text", "text": "b"}}}
    token, form_token, value = decode_action(clicked)
    assert (token, form_token, value) == ("T2", "", "b")


def test_user_picker_round_trips_the_picked_id() -> None:
    action = Action(id="sel", label="Who?", kind="user_select", context={"token": "T3"})
    el = _button_element(build_action_blocks("Assign to", [action]))
    assert el["type"] == "users_select"
    assert "options" not in el  # the workspace supplies them
    clicked = {**el, "selected_user": "U0777"}
    assert decode_action(clicked) == ("T3", "", "U0777")
    assert picked_kind(clicked) == "user"  # -> the engine resolves the id to a name


def test_channel_picker_round_trips_the_picked_id() -> None:
    action = Action(id="sel", label="Where?", kind="channel_select", context={"token": "T4"})
    el = _button_element(build_action_blocks("Post to", [action]))
    assert el["type"] == "channels_select"
    clicked = {**el, "selected_channel": "C0123"}
    assert decode_action(clicked) == ("T4", "", "C0123")
    assert picked_kind(clicked) == "channel"


def test_a_button_is_never_mistaken_for_a_pick() -> None:
    # A button's own "value" is our JSON envelope, not a picked value.
    action = Action(id="opt0", label="Yes", value="Yes", context={"token": "T5"})
    el = _button_element(build_action_blocks("Choose", [action]))
    assert picked_kind(el) == ""
    assert decode_action(el) == ("T5", "", "Yes")


def test_unique_action_ids_per_element() -> None:
    actions = [Action(id=f"o{i}", label=str(i), value=str(i), context={"token": "T"}) for i in range(3)]
    ids = [e["action_id"] for e in build_action_blocks("q", actions)[1]["elements"]]
    assert len(set(ids)) == 3  # Slack requires action_ids unique within a message


def test_modal_view_and_submission_extraction() -> None:
    form = Form(
        title="Bug report",
        fields=(
            FormField(name="summary", label="Summary"),
            FormField(name="details", label="Details", type="textarea"),
            FormField(name="severity", label="Severity", type="select", options=("low", "high")),
            FormField(name="blocking", label="Blocking?", type="bool"),
        ),
    )
    view = build_modal_view(form, state="FTOKEN")
    assert view["type"] == "modal"
    assert view["callback_id"] == FORM_CALLBACK
    assert view["private_metadata"] == "FTOKEN"
    assert [b["block_id"] for b in view["blocks"]] == ["summary", "details", "severity", "blocking"]
    assert view["blocks"][1]["element"]["multiline"] is True  # textarea
    assert view["blocks"][3]["element"]["type"] == "checkboxes"  # bool -> one checkbox

    # what Slack sends on submit (block_id == action_id == field name):
    state = {
        "values": {
            "summary": {"summary": {"type": "plain_text_input", "value": "crash"}},
            "details": {"details": {"type": "plain_text_input", "value": "stack..."}},
            "severity": {"severity": {"type": "static_select", "selected_option": {"value": "high"}}},
            "blocking": {"blocking": {"type": "checkboxes", "selected_options": [{"value": "yes"}]}},
        }
    }
    assert extract_submission(state) == {
        "summary": "crash",
        "details": "stack...",
        "severity": "high",
        "blocking": "yes",
    }


# --- the full field vocabulary -------------------------------------------------

# Every neutral type and the Block Kit element it must become.
FIELD_ELEMENTS = [
    ("text", "plain_text_input"),
    ("textarea", "plain_text_input"),
    ("number", "number_input"),
    ("email", "email_text_input"),
    ("url", "url_text_input"),
    ("tel", "plain_text_input"),
    ("select", "static_select"),
    ("multiselect", "multi_static_select"),
    ("radio", "radio_buttons"),
    ("bool", "checkboxes"),
    ("user", "users_select"),
    ("users", "multi_users_select"),
    ("channel", "channels_select"),
    ("channels", "multi_channels_select"),
    ("date", "datepicker"),
    ("datetime", "datetimepicker"),
    ("time", "timepicker"),
]


@pytest.mark.parametrize(("field_type", "element_type"), FIELD_ELEMENTS)
def test_every_field_type_renders_its_element(field_type: str, element_type: str) -> None:
    options = ("a", "b") if field_type in ("select", "multiselect", "radio") else ()
    form = Form(title="T", fields=(FormField(name="f", label="F", type=field_type, options=options),))
    block = build_modal_view(form, state="S")["blocks"][0]
    assert block["type"] == "input"
    assert block["element"]["type"] == element_type
    assert block["element"]["action_id"] == "f"


def test_number_input_declares_its_required_field() -> None:
    # Slack rejects the WHOLE view if a number_input omits is_decimal_allowed
    # (caught live: views.open -> invalid_arguments).
    form = Form(title="T", fields=(FormField(name="n", label="How many", type="number"),))
    element = build_modal_view(form, state="S")["blocks"][0]["element"]
    assert element["is_decimal_allowed"] is True


def test_choices_and_hint_reach_the_element() -> None:
    form = Form(title="T", fields=(
        FormField(name="tags", label="Tags", type="multiselect", options=("a", "b"),
                  help_text="pick as many as apply"),
    ))
    block = build_modal_view(form, state="S")["blocks"][0]
    assert [o["value"] for o in block["element"]["options"]] == ["a", "b"]
    assert block["hint"]["text"] == "pick as many as apply"


def test_label_field_is_static_text_not_an_input() -> None:
    form = Form(title="T", fields=(
        FormField(name="note", label="*Read this first*", type="label"),
        FormField(name="who", label="Who", type="user"),
    ))
    blocks = build_modal_view(form, state="S")["blocks"]
    assert blocks[0] == {"type": "section", "text": {"type": "mrkdwn", "text": "*Read this first*"}}
    assert blocks[1]["type"] == "input"


def test_submission_reads_every_answer_shape() -> None:
    # The state shapes Slack sends, one per element family.
    state = {
        "values": {
            "note": {"note": {"type": "number_input", "value": "42"}},
            "tags": {"tags": {"type": "multi_static_select",
                              "selected_options": [{"value": "a"}, {"value": "b"}]}},
            "who": {"who": {"type": "users_select", "selected_user": "U1"}},
            "team": {"team": {"type": "multi_users_select", "selected_users": ["U1", "U2"]}},
            "where": {"where": {"type": "channels_select", "selected_channel": "C1"}},
            "rooms": {"rooms": {"type": "multi_channels_select", "selected_channels": ["C1", "C2"]}},
            "when": {"when": {"type": "datepicker", "selected_date": "2026-08-04"}},
            "at": {"at": {"type": "timepicker", "selected_time": "09:30"}},
            "start": {"start": {"type": "datetimepicker", "selected_date_time": 1785000000}},
            "level": {"level": {"type": "radio_buttons", "selected_option": {"value": "high"}}},
            "skipped": {"skipped": {"type": "plain_text_input", "value": None}},
        }
    }
    assert extract_submission(state) == {
        "note": "42",
        "tags": "a, b",
        "who": "U1",
        "team": "U1, U2",
        "where": "C1",
        "rooms": "C1, C2",
        "when": "2026-08-04",
        "at": "09:30",
        "start": "2026-07-25T17:20+00:00",  # unix seconds -> ISO
        "level": "high",
        "skipped": "",
    }


def test_the_element_table_covers_the_whole_vocabulary() -> None:
    # A type added to FIELD_TYPES but not to the mapping would silently render as
    # a plain text input. Same idea for the widget kinds.
    from crucible.gateways.slack.rendering import _FIELD_ELEMENTS, _MENU_TYPES
    from crucible.ports.chat.types import (
        ACTION_BUTTON,
        ACTION_KINDS,
        FIELD_TYPES,
        STATIC_FIELD_TYPES,
    )

    assert set(_FIELD_ELEMENTS) == set(FIELD_TYPES) - STATIC_FIELD_TYPES
    assert set(_MENU_TYPES) == set(ACTION_KINDS) - {ACTION_BUTTON}
