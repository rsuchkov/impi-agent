"""Slack Block Kit rendering + the widget token round-trip (pure, offline)."""

from crucible.gateways.slack.rendering import (
    FORM_CALLBACK,
    build_action_blocks,
    build_modal_view,
    decode_action,
    extract_submission,
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
    assert view["blocks"][3]["element"]["type"] == "static_select"  # bool -> yes/no

    # what Slack sends on submit (block_id == action_id == field name):
    state = {
        "values": {
            "summary": {"summary": {"type": "plain_text_input", "value": "crash"}},
            "details": {"details": {"type": "plain_text_input", "value": "stack..."}},
            "severity": {"severity": {"type": "static_select", "selected_option": {"value": "high"}}},
            "blocking": {"blocking": {"type": "static_select", "selected_option": {"value": "yes"}}},
        }
    }
    assert extract_submission(state) == {
        "summary": "crash",
        "details": "stack...",
        "severity": "high",
        "blocking": "yes",
    }
