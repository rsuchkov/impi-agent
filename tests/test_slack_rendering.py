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
from crucible.ports.chat.types import Action, Card, Choice, Form, FormField


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
    action = Action(id="sel", label="Pick", kind="select", options=Choice.of("a", "b"), context={"token": "T2"})
    blocks = build_action_blocks("Choose", [action])
    el = _button_element(blocks)
    assert el["type"] == "static_select"
    assert blocks[1]["block_id"] == "tok:T2"  # on the block, per Block Kit
    # simulate the block_actions payload Slack sends when "b" is picked: it stamps
    # the containing block's id onto the action.
    clicked = {**el, "block_id": blocks[1]["block_id"],
               "selected_option": {"value": "b", "text": {"type": "plain_text", "text": "b"}}}
    token, form_token, value = decode_action(clicked)
    assert (token, form_token, value) == ("T2", "", "b")


def test_user_picker_round_trips_the_picked_id() -> None:
    action = Action(id="sel", label="Who?", kind="user_select", context={"token": "T3"})
    blocks = build_action_blocks("Assign to", [action])
    el = _button_element(blocks)
    assert el["type"] == "users_select"
    assert "options" not in el  # the workspace supplies them
    clicked = {**el, "block_id": blocks[1]["block_id"], "selected_user": "U0777"}
    assert decode_action(clicked) == ("T3", "", "U0777")
    assert picked_kind(clicked) == "user"  # -> the engine resolves the id to a name


def test_channel_picker_round_trips_the_picked_id() -> None:
    action = Action(id="sel", label="Where?", kind="channel_select", context={"token": "T4"})
    blocks = build_action_blocks("Post to", [action])
    el = _button_element(blocks)
    assert el["type"] == "channels_select"
    clicked = {**el, "block_id": blocks[1]["block_id"], "selected_channel": "C0123"}
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


# --- where block_id belongs ------------------------------------------------------


def test_a_menu_puts_its_token_on_the_block_not_the_element() -> None:
    # Slack rejects the whole message when block_id appears on an element:
    # "invalid additional property: block_id [json-pointer:/blocks/1/elements/0]"
    # (caught live against chat.postMessage).
    action = Action(id="sel", label="Pick", kind="select",
                    options=Choice.of("a", "b"), context={"token": "T1"})

    blocks = build_action_blocks("Choose", [action])

    assert blocks[1]["type"] == "actions"
    assert blocks[1]["block_id"] == "tok:T1"
    assert "block_id" not in blocks[1]["elements"][0]


def test_a_picker_puts_its_token_on_the_block_too() -> None:
    for kind in ("user_select", "channel_select"):
        blocks = build_action_blocks(
            "Who?", [Action(id="sel", label="Pick", kind=kind, context={"token": "T2"})]
        )
        assert blocks[1]["block_id"] == "tok:T2"
        assert "block_id" not in blocks[1]["elements"][0], kind


def test_buttons_alone_need_no_block_id() -> None:
    blocks = build_action_blocks(
        "Choose", [Action(id="y", label="Yes", value="Yes", context={"token": "T3"})]
    )
    assert "block_id" not in blocks[1]  # a button carries its token in `value`


def test_the_token_still_round_trips_from_the_block() -> None:
    # Slack echoes the containing block's block_id on every action it delivers.
    incoming = {"type": "static_select", "block_id": "tok:T1",
                "selected_option": {"value": "a"}}
    assert decode_action(incoming) == ("T1", "", "a")


def test_a_card_menu_keeps_its_screen_state_on_the_block() -> None:
    from crucible.gateways.slack.rendering import build_card_blocks
    from crucible.interactions.screens import ScreenState, screen_action

    state = ScreenState(screen="skills", agent="assistant", data={"page": 1})
    card = Card(
        text="**greek-drill**",
        actions=(
            screen_action(state, id="open", label="Details", value="open:greek-drill"),
            screen_action(state, id="give", label="Give it to…", kind="select",
                          options=(Choice(label="tutor", value="assign:greek-drill:tutor"),)),
        ),
    )

    blocks = build_card_blocks([card])

    actions_block = blocks[1]
    assert actions_block["block_id"].startswith("scr:skills|")
    assert all("block_id" not in e for e in actions_block["elements"])
    # And a click on either control still routes to the screen.
    from crucible.gateways.slack.rendering import decode_screen
    picked = {"type": "static_select", "block_id": actions_block["block_id"],
              "selected_option": {"value": "assign:greek-drill:tutor"}}
    screen, raw = decode_screen(picked)
    assert screen == "skills" and ScreenState.decode(raw) == state


def test_a_prefilled_field_arrives_as_slacks_initial_value() -> None:
    """Same promise as the Mattermost dialog's `default`, spelled Slack's way:
    an edit form shows what is there now inside the control."""
    from crucible.gateways.slack.rendering import _input_element
    from crucible.ports.chat.types import FormField

    text = _input_element(FormField(name="who", label="Who", value="assistant"))
    assert text["initial_value"] == "assistant"

    chosen = _input_element(
        FormField(name="how", label="How", type="select",
                  options=("always", "never"), value="never")
    )
    assert chosen["initial_option"]["value"] == "never"

    picked = _input_element(
        FormField(name="many", label="Many", type="multiselect",
                  options=("a", "b", "c"), value="a, c")
    )
    assert [o["value"] for o in picked["initial_options"]] == ["a", "c"]


def test_a_prefill_that_names_no_option_is_dropped_rather_than_sent() -> None:
    """Slack rejects the whole view when an initial_option is not among the
    offered ones, so a stale value must cost the prefill and not the form."""
    from crucible.gateways.slack.rendering import _input_element
    from crucible.ports.chat.types import FormField

    element = _input_element(
        FormField(name="how", label="How", type="select",
                  options=("always", "never"), value="whenever")
    )
    assert "initial_option" not in element
    assert [o["value"] for o in element["options"]] == ["always", "never"]


def test_card_text_is_converted_like_every_other_string_that_reaches_slack() -> None:
    """A card's text is Markdown, the same as a reply's. It went into the mrkdwn
    section verbatim, so the tool gate's `**agent**` showed its asterisks while
    the `tool` beside it looked right — backticks mean the same in both
    languages, which is what let this sit unnoticed."""
    from crucible.gateways.slack.rendering import build_card_blocks
    from crucible.ports.chat.types import Card

    blocks = build_card_blocks(
        [Card(text="⚙️ Allowed once — **omni** running `jira_create_issue`.")]
    )
    rendered = blocks[0]["text"]["text"]
    assert "*omni*" in rendered
    assert "**" not in rendered
    assert "`jira_create_issue`" in rendered


def test_the_notification_line_is_converted_too() -> None:
    """It is what a client that cannot render blocks shows, and what a phone
    puts in the notification."""
    from crucible.gateways.slack.client import _fallback_text
    from crucible.ports.chat.types import Card

    assert _fallback_text([Card(text="**omni** wants in")]) == "*omni* wants in"


def test_converting_is_not_done_twice_on_the_same_string() -> None:
    """The guard behind converting at each caller rather than inside _rewrite:
    mrkdwn `*bold*` fed back through the formatter would come out `_bold_`,
    because that is the italic rule doing its job."""
    from crucible.gateways.slack.formatter import markdown_to_mrkdwn

    once = markdown_to_mrkdwn("**omni**")
    assert once == "*omni*"
    assert markdown_to_mrkdwn(once) == "_omni_"  # exactly what must not happen


def test_a_confirmation_card_becomes_native_blocks_from_the_shared_layout() -> None:
    from crucible.gateways.slack.rendering import build_confirmation_blocks
    from crucible.ports.chat.types import ApprovalCard, PreviewRow

    card = ApprovalCard(
        agent="assistant", headline="is asking for a secret.", icon="🔐",
        fields=(PreviewRow("Secret", "github-token"), PreviewRow("Reason", "push\nrelease *now*")),
        block_label="Command", block="gh release create v1",
        expires_at="2026-10-04T12:00:00+00:00",
    )
    allow = Action(id="once", label="Allow once", value="once", context={"approval": "ap-1"})
    blocks = build_confirmation_blocks(card, [allow])

    # No title: the agent and the engine's sentence are the heading, as plain text.
    assert blocks[0] == {"type": "header", "text": {"type": "plain_text", "text": "🔐 assistant is asking for a secret."}}
    fields = blocks[1]["fields"]
    assert fields[0]["text"] == "*Secret:* github-token"  # plain, not inline code
    assert fields[1]["text"] == "*Reason:* push release *now*"  # a line break is a space, not a glue
    assert blocks[2]["text"]["text"] == "*Command*\n```\ngh release create v1\n```"
    assert blocks[3]["type"] == "context"
    assert blocks[3]["elements"][0]["text"].startswith("Answer by <!date^1791115200")  # the agent is in the heading already
    assert blocks[4]["type"] == "actions"

    # Answered: the verdict is the heading (the engine rewrote the sentence), no
    # buttons, no deadline — and nothing left to sign with.
    closed = build_confirmation_blocks(
        ApprovalCard(**{**card.__dict__, "headline": "— Allowed once: github-token.", "verdict": "allowed_once", "expires_at": ""}), []
    )
    assert closed[0]["text"]["text"] == "🔐 assistant — Allowed once: github-token."
    assert [b["type"] for b in closed] == ["header", "section", "section"]


def test_a_titled_card_leads_with_the_title_and_shows_a_passage_whole() -> None:
    from crucible.gateways.slack.rendering import build_confirmation_blocks
    from crucible.ports.chat.types import ApprovalCard, PreviewRow

    card = ApprovalCard(
        agent="jira-assistant-slack", headline="wants to run add_comment.", title="Add a comment to PROJ-123",
        tool="add_comment",
        fields=(
            PreviewRow("Issue", "PROJ-123 — Fix the login page"),
            PreviewRow("Comment", "Looks good.\n\nRegards, <Roman> & team", passage=True),
        ),
        expires_at="2026-10-04T12:00:00+00:00",
    )
    blocks = build_confirmation_blocks(card, [Action(id="once", label="Allow once", value="once")])
    assert blocks[0] == {"type": "header", "text": {"type": "plain_text", "text": "⚙️ Add a comment to PROJ-123"}}
    assert blocks[1]["fields"] == [{"type": "mrkdwn", "text": "*Issue:* PROJ-123 — Fix the login page"}]
    # The passage: whole, its lines kept as a quote, mrkdwn escaped.
    assert blocks[2]["text"]["text"] == "*Comment*\n> Looks good.\n> \n> Regards, &lt;Roman&gt; &amp; team"
    # The tool's id is a signature, not a fact.
    assert blocks[3]["elements"][0]["text"].startswith("jira-assistant-slack · add_comment · Answer by")


def test_a_long_passage_or_block_goes_on_in_further_sections_each_fenced() -> None:
    from crucible.gateways.slack.rendering import (
        _SECTION_MAX,
        build_confirmation_blocks,
    )
    from crucible.ports.chat.types import ApprovalCard, PreviewRow

    script = "\n".join(f"line {n}: " + "x" * 90 for n in range(100))  # ~10k chars
    card = ApprovalCard(
        agent="a", headline="h", fields=(PreviewRow("Body", script, passage=True),),
        block_label="Script", block=script,
    )
    blocks = build_confirmation_blocks(card, [])
    passages = [b for b in blocks if b["type"] == "section" and "text" in b and b["text"]["text"].startswith(("*Body*", "> "))]
    fenced = [b for b in blocks if b["type"] == "section" and "text" in b and "```" in b["text"]["text"]]
    assert len(passages) >= 3 and all(len(b["text"]["text"]) <= _SECTION_MAX for b in passages)
    assert len(fenced) >= 3 and all(b["text"]["text"].rstrip().endswith("```") for b in fenced)
    assert all(b["text"]["text"].lstrip("*Script*\n").startswith("```") for b in fenced)
    assert any("cut at 8000 of" in b["text"]["text"] for b in blocks)  # containment's own mark


def test_slack_card_values_cannot_mention_link_or_break_their_code_span() -> None:
    """mrkdwn reads `<…>` as a link or a mention and `&` as an entity — so the
    values are escaped for it, not only contained; a block's backticks are
    defused so it cannot close its own fence."""
    from crucible.gateways.slack.rendering import build_confirmation_blocks
    from crucible.ports.chat.types import ApprovalCard, PreviewRow

    card = ApprovalCard(
        agent="assistant", headline="wants to run dangerous.",
        fields=(
            PreviewRow("Action", "<!channel> see <https://evil.example|jira> & co"),
            PreviewRow("Reason", "has a ` backtick"),
            PreviewRow("Assignee", "", before="alice"),  # a change to nothing
        ),
        block_label="Arguments", block='{"url": "<https://evil.example>"}\n```\nnot a fence',
        expires_at="2026-10-04T12:00:00+00:00",
    )
    blocks = build_confirmation_blocks(card, [Action(id="once", label="Allow once", value="once")])
    fields = blocks[1]["fields"]
    assert fields[0]["text"] == "*Action:* &lt;!channel&gt; see &lt;https://evil.example|jira&gt; &amp; co"
    assert fields[1]["text"] == "*Reason:* has a ` backtick"
    assert fields[2]["text"] == "*Assignee:* alice → "
    hostile = build_confirmation_blocks(
        ApprovalCard(agent="a", headline="h", title="<!channel> *bold* <https://evil.example|x>"), []
    )
    assert hostile[0]["text"] == {"type": "plain_text", "text": "⚙️ <!channel> *bold* <https://evil.example|x>"}  # literal
    body = blocks[2]["text"]["text"]
    assert "&lt;https://evil.example&gt;" in body
    assert body.count("```") == 2  # the fence is ours; the one inside was defused to ` ` `
    # No title: the heading names the agent, so only the deadline signs the card.
    assert blocks[3]["elements"][0]["text"] == "Answer by <!date^1791115200^{date_short_pretty} at {time}|2026-10-04T12:00:00+00:00>"


