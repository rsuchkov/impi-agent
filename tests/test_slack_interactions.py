"""SlackInteractions (crucible/gateways/slack/interactions.py): what a click, a
modal submission, a message shortcut and a slash command become on the way to
the neutral dispatcher — offline, against a real AsyncApp and recorded fakes.

Half of these moved here from test_slack_gateway.py when the interactive half
left the gateway; the rest cover what the gateway never had: slash commands,
and a word for whoever clicks a request that is not theirs to answer.
"""

import json
from types import SimpleNamespace

import pytest
from slack_bolt.async_app import AsyncApp

from crucible.approvals import ApprovalOutcome
from crucible.gateways.slack.interactions import SlackInteractions
from crucible.gateways.slack.rendering import FORM_CALLBACK
from crucible.interactions.results import ActionResult
from crucible.interactions.screens import ScreenOpened
from crucible.ports.chat.types import (
    AGENT_UNAVAILABLE_TEXT,
    COMMAND_ACK_TEXT,
    NOT_AN_APPROVER_TEXT,
    Form,
    FormField,
)
from tests.fakes.slack import FakeDispatcher, FakePoster, Respond

_APPS: list[AsyncApp] = []


@pytest.fixture(autouse=True)
async def _close_apps():
    yield
    for app in _APPS:
        try:
            await app.client.session.close()  # type: ignore[union-attr]
        except Exception:
            pass
    _APPS.clear()


def _interactions(dispatcher, poster=None, **kwargs) -> SlackInteractions:
    app = AsyncApp(token="xoxb-fake", signing_secret="x" * 16)
    _APPS.append(app)
    it = SlackInteractions(app, dispatcher, poster or FakePoster(), agent=kwargs.pop("agent", "assistant"), **kwargs)  # type: ignore[arg-type]
    it.register()
    return it


def _recorder(it: SlackInteractions, method: str) -> list[dict]:
    calls: list[dict] = []

    async def _rec(**kwargs):
        calls.append(kwargs)
        return {"ok": True}

    setattr(it._app.client, method, _rec)
    return calls


def _button(**value) -> dict:
    return {"type": "button", "action_id": "cruxw0", "value": json.dumps({"token": "", "form": "", "value": "", **value})}


# --- clicks --------------------------------------------------------------------------


async def test_button_click_falls_through_to_consume_action() -> None:
    dispatcher = FakeDispatcher(pending_ok=False)
    it = _interactions(dispatcher)
    await it.handle_action({"user": {"id": "U2"}, "actions": [_button(token="TK", value="Yes")]})
    assert ("resolve_pending", "TK", "Yes") in dispatcher.calls
    assert ("consume_action", "TK", "Yes", "U2") in dispatcher.calls


async def test_button_click_resolving_pending_skips_consume() -> None:
    dispatcher = FakeDispatcher(pending_ok=True)  # a blocking request was waiting
    it = _interactions(dispatcher)
    await it.handle_action({"user": {"id": "U2"}, "actions": [_button(token="TK", value="Allow")]})
    assert ("resolve_pending", "TK", "Allow") in dispatcher.calls
    assert not any(c[0] == "consume_action" for c in dispatcher.calls)


async def test_button_click_strips_the_buttons() -> None:
    # After a fire-and-forget click, Slack won't retire the buttons — the message
    # is updated (blocks dropped) so it can't be clicked again.
    it = _interactions(FakeDispatcher())
    updates = _recorder(it, "chat_update")
    await it.handle_action({
        "user": {"id": "U2"}, "channel": {"id": "C1"}, "message": {"ts": "9.9"},
        "actions": [_button(token="TK", value="Yes")],
    })
    assert updates == [{"channel": "C1", "ts": "9.9", "text": "Selected: Yes", "blocks": []}]


async def test_form_open_click_opens_modal() -> None:
    form = SimpleNamespace(agent="assistant", form=Form(title="T", fields=(FormField(name="s", label="S"),)))
    dispatcher = FakeDispatcher(form=form)
    poster = FakePoster()
    it = _interactions(dispatcher, poster)
    await it.handle_action({"user": {"id": "U2"}, "trigger_id": "TRIG", "actions": [_button(form="F1")]})
    assert ("load_form", "F1") in dispatcher.calls
    assert poster.opened and poster.opened[0][0] == "TRIG" and poster.opened[0][2] == "F1"


async def test_form_open_leaves_the_button_alive_for_a_second_try() -> None:
    # Closing the modal without submitting must not cost the button: it is retired
    # when the form is answered, not when it is opened.
    form = SimpleNamespace(agent="assistant", form=Form(title="T", fields=(FormField(name="s", label="S"),)))
    it = _interactions(FakeDispatcher(form=form))
    updates = _recorder(it, "chat_update")
    await it.handle_action({
        "user": {"id": "U2"}, "trigger_id": "TRIG",
        "channel": {"id": "C1"}, "message": {"ts": "1.1"},
        "actions": [_button(form="F1")],
    })
    assert updates == []  # the message was left untouched


async def test_form_open_retires_a_button_whose_form_is_gone() -> None:
    # Already answered (or expired): say so instead of leaving a dead button.
    it = _interactions(FakeDispatcher(form=None))
    updates = _recorder(it, "chat_update")
    await it.handle_action({
        "user": {"id": "U2"}, "trigger_id": "TRIG",
        "channel": {"id": "C1"}, "message": {"ts": "1.1"},
        "actions": [_button(form="F1")],
    })
    assert updates and "no longer active" in updates[0]["text"]
    assert updates[0]["blocks"] == []


async def test_view_submission_feeds_form_values() -> None:
    dispatcher = FakeDispatcher()
    it = _interactions(dispatcher)
    await it.handle_view({
        "user": {"id": "U2"},
        "view": {
            "callback_id": FORM_CALLBACK,
            "private_metadata": "FTOK",
            "state": {"values": {"s": {"s": {"type": "plain_text_input", "value": "hello"}}}},
        },
    })
    assert ("submit_form", "FTOK", {"s": "hello"}, False, "U2") in dispatcher.calls


# --- a request for a credential ------------------------------------------------------

_APPROVAL_CLICK = {
    "user": {"id": "U9"}, "channel": {"id": "D1"}, "message": {"ts": "5.5"},
    "actions": [{"type": "button", "action_id": "cruxw0",
                 "value": json.dumps({"token": "", "form": "", "value": "once", "approval": "APR"})}],
}


async def test_an_approver_s_click_is_resolved_and_the_card_is_left_to_its_owner() -> None:
    dispatcher = FakeDispatcher(approval=ApprovalOutcome.RESOLVED)
    it = _interactions(dispatcher)
    updates = _recorder(it, "chat_update")
    whispers = _recorder(it, "chat_postEphemeral")
    await it.handle_action(_APPROVAL_CLICK)
    assert ("resolve_approval", "APR", "once", "U9") in dispatcher.calls
    assert updates == [] and whispers == []  # the broker rewrites its own card


async def test_a_stranger_s_click_is_told_off_privately_and_the_card_stays_live() -> None:
    """The HTTP receiver answers such a click with an ephemeral notice; over the
    socket nothing did, and a stranger clicking a request addressed to someone
    else saw a button that did nothing at all."""
    it = _interactions(FakeDispatcher(approval=ApprovalOutcome.NOT_ALLOWED))
    updates = _recorder(it, "chat_update")
    whispers = _recorder(it, "chat_postEphemeral")
    await it.handle_action(_APPROVAL_CLICK)
    assert whispers == [{"channel": "D1", "user": "U9", "text": NOT_AN_APPROVER_TEXT}]
    assert updates == []


async def test_a_click_on_a_request_that_is_gone_retires_the_buttons() -> None:
    it = _interactions(FakeDispatcher(approval=ApprovalOutcome.NOT_MINE))
    updates = _recorder(it, "chat_update")
    await it.handle_action(_APPROVAL_CLICK)
    assert updates and updates[0]["blocks"] == [] and "no longer active" in updates[0]["text"]


# --- message shortcuts (the thread-aware command entry) --------------------------------

# A real Slack payload (captured live): a crux_ shortcut used on a message that
# lives inside a thread. Slack forbids custom slash commands in threads, so this
# is the only entry that carries thread context.
SHORTCUT_IN_THREAD = {
    "type": "message_action",
    "callback_id": "crux_summarize",
    "channel": {"id": "C0BC51KQWTX", "name": "privategroup"},
    "user": {"id": "U0HNU8P60", "username": "roman.suchkov", "name": "roman.suchkov"},
    "message_ts": "1782309753.848289",
    "message": {
        "ts": "1782309753.848289",
        "thread_ts": "1782309611.465749",
        "text": "a reply in the thread",
        "user": "U0HNU8P60",
    },
    "trigger_id": "11739525603684.6556695416068.842c0fee722",
}


async def test_shortcut_in_thread_invokes_command_on_the_thread() -> None:
    dispatcher = FakeDispatcher()
    _interactions(dispatcher).handle_shortcut(SHORTCUT_IN_THREAD)
    call = dispatcher.calls[0]
    assert call[0] == "invoke_command" and call[1] == "assistant"
    assert call[2] == "C0BC51KQWTX"  # channel
    assert call[3] == "1782309611.465749"  # conversation = the thread root
    assert call[4] == "thread"
    assert call[5] == "/summarize"  # callback id minus the crux_ prefix
    assert call[6] == "U0HNU8P60" and call[7] == "roman.suchkov"


async def test_shortcut_on_a_top_level_message_uses_that_message_as_root() -> None:
    dispatcher = FakeDispatcher()
    _interactions(dispatcher).handle_shortcut({**SHORTCUT_IN_THREAD, "message": {"ts": "111.2", "text": "top level"}})
    call = dispatcher.calls[0]
    assert call[3] == "111.2" and call[4] == "thread"  # a reply would start this thread


async def test_shortcut_in_a_dm_runs_as_the_dm_conversation() -> None:
    dispatcher = FakeDispatcher()
    _interactions(dispatcher).handle_shortcut({
        **SHORTCUT_IN_THREAD, "channel": {"id": "D0123", "name": "dm"}, "message": {"ts": "111.2", "text": "hi"},
    })
    call = dispatcher.calls[0]
    assert call[3] == "D0123" and call[4] == "dm"


async def test_shortcut_without_a_command_name_is_ignored() -> None:
    dispatcher = FakeDispatcher()
    _interactions(dispatcher).handle_shortcut({**SHORTCUT_IN_THREAD, "callback_id": "crux_"})
    assert dispatcher.calls == []


async def test_command_prefix_is_configurable() -> None:
    # A workspace with its own shortcut naming: the prefix is config, and the
    # command name is whatever follows it.
    dispatcher = FakeDispatcher()
    _interactions(dispatcher, command_prefix="acme-").handle_shortcut({**SHORTCUT_IN_THREAD, "callback_id": "acme-summarize"})
    assert dispatcher.calls[0][5] == "/summarize"


async def test_empty_prefix_makes_the_callback_id_the_command() -> None:
    dispatcher = FakeDispatcher()
    _interactions(dispatcher, command_prefix="").handle_shortcut({**SHORTCUT_IN_THREAD, "callback_id": "summarize"})
    assert dispatcher.calls[0][5] == "/summarize"


# --- slash commands (the entry everywhere but a thread) ---------------------------------

# The shape bolt hands a slash-command handler, as Slack sends it.
def _command(command="/ward", channel="D0AB1", channel_name="directmessage", text="", user="U0HNU8P60"):
    return {
        "command": command, "text": text,
        "channel_id": channel, "channel_name": channel_name,
        "user_id": user, "user_name": "roman.suchkov",
        "trigger_id": "11739525603684.6556695416068.842c0fee722",
    }


async def test_a_command_a_screen_owns_is_drawn_by_the_engine_and_answered_with_silence() -> None:
    """/ward in the operator's direct message: the screen is opened as the
    agent this app is, in the DM as the conversation. No turn, no receipt —
    the card the screen posted is the answer."""
    dispatcher = FakeDispatcher(opened=ScreenOpened(owned=True))
    respond = Respond()
    await _interactions(dispatcher, agent="ward").handle_command(_command(), respond)
    assert dispatcher.calls == [("open_screen", "ward", "ward", "D0AB1", "D0AB1", "dm", "U0HNU8P60")]
    assert respond.said == []


async def test_a_screen_s_refusal_reaches_only_the_person_who_typed_the_command() -> None:
    dispatcher = FakeDispatcher(opened=ScreenOpened(owned=True, refused="This works in a direct message with me."))
    respond = Respond()
    await _interactions(dispatcher, agent="ward").handle_command(_command(channel="C77", channel_name="general"), respond)
    assert respond.said == ["This works in a direct message with me."]
    assert not any(c[0] == "invoke_command" for c in dispatcher.calls)


async def test_a_command_nobody_owns_becomes_a_turn_of_the_agent_with_a_receipt() -> None:
    dispatcher = FakeDispatcher(command_result=ActionResult.FED)
    respond = Respond()
    await _interactions(dispatcher).handle_command(_command(command="/summarize", channel="C77", channel_name="general", text="last week"), respond)
    call = next(c for c in dispatcher.calls if c[0] == "invoke_command")
    assert call[1:] == ("assistant", "C77", "C77", "channel", "/summarize last week", "U0HNU8P60", "roman.suchkov")
    assert respond.said == [COMMAND_ACK_TEXT]


async def test_a_command_for_an_agent_with_no_presence_says_so() -> None:
    dispatcher = FakeDispatcher(command_result=ActionResult.UNAVAILABLE)
    respond = Respond()
    await _interactions(dispatcher).handle_command(_command(command="/summarize", channel="C77", channel_name="general"), respond)
    assert respond.said == [AGENT_UNAVAILABLE_TEXT]


async def test_a_command_without_a_name_is_ignored() -> None:
    dispatcher = FakeDispatcher()
    await _interactions(dispatcher).handle_command(_command(command="/"), Respond())
    assert dispatcher.calls == []
