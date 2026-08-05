"""SlackGateway message decision + interactive dispatch (offline).

Constructs a real AsyncApp/socket handler (cheap, no network) inside the running
test loop, then drives the gateway's internal handlers directly.
"""

import json
from types import SimpleNamespace

import pytest
from slack_bolt.async_app import AsyncApp

from crucible.gateways.slack.gateway import SlackGateway
from crucible.gateways.slack.rendering import FORM_CALLBACK
from crucible.ports.chat.types import Form, FormField

OWN = "UBOT"

# Each AsyncApp/socket handler opens an aiohttp session; close them after each test
# so the suite stays free of "Unclosed client session" noise.
_HANDLERS: list = []


@pytest.fixture(autouse=True)
async def _close_handlers():
    yield
    for handler in _HANDLERS:
        try:
            await handler.close_async()
        except Exception:
            pass
    _HANDLERS.clear()


class FakeSink:
    def __init__(self) -> None:
        self.submitted: list = []

    def submit(self, msg, chat) -> None:
        self.submitted.append(msg)


class FakeDispatcher:
    def __init__(self, *, form=None, pending_ok=False) -> None:
        self.calls: list = []
        self.picks: list[str] = []  # the pick kind of each consumed action
        self._form = form
        self._pending_ok = pending_ok

    def resolve_pending(self, token, value) -> bool:
        self.calls.append(("resolve_pending", token, value))
        return self._pending_ok

    async def consume_action(self, token, value, user_id, *, pick=""):
        self.calls.append(("consume_action", token, value, user_id))
        self.picks.append(pick)

    async def load_form(self, form_token):
        self.calls.append(("load_form", form_token))
        return self._form

    async def submit_form(self, state, submission, cancelled, user_id):
        self.calls.append(("submit_form", state, submission, cancelled, user_id))

    def invoke_command(self, agent, *, channel_id, conversation_id, kind, text, user_id, username=""):
        self.calls.append(
            ("invoke_command", agent, channel_id, conversation_id, kind, text, user_id, username)
        )


class FakePoster:
    def __init__(self) -> None:
        self.opened: list = []

    async def open_dialog(self, trigger_id, form, *, submit_url, state) -> None:
        self.opened.append((trigger_id, form, state))


async def _ack() -> None:
    return None


def _gateway(sink, dispatcher=None, poster=None, **kwargs):
    app = AsyncApp(token="xoxb-fake", signing_secret="x" * 16)
    gw = SlackGateway(
        app, "xapp-fake", sink, object(), poster=poster, dispatcher=dispatcher, **kwargs  # type: ignore[arg-type]
    )
    _HANDLERS.append(gw._handler)
    gw._own_user_id = OWN
    return gw


async def test_dm_message_is_submitted() -> None:
    sink = FakeSink()
    gw = _gateway(sink)
    await gw._handle_message(
        {"channel": "D1", "channel_type": "im", "ts": "1.0", "user": "U2", "text": "hi"}
    )
    assert len(sink.submitted) == 1
    assert sink.submitted[0].is_dm is True


async def test_channel_without_mention_is_ignored() -> None:
    sink = FakeSink()
    gw = _gateway(sink)
    await gw._handle_message(
        {"channel": "C1", "channel_type": "channel", "ts": "1.0", "user": "U2", "text": "hello"}
    )
    assert sink.submitted == []


async def test_channel_mention_is_submitted() -> None:
    sink = FakeSink()
    gw = _gateway(sink)
    await gw._handle_message(
        {"channel": "C1", "channel_type": "channel", "ts": "1.0", "user": "U2", "text": f"<@{OWN}> hi"}
    )
    assert len(sink.submitted) == 1


async def test_button_click_falls_through_to_consume_action() -> None:
    dispatcher = FakeDispatcher(pending_ok=False)
    gw = _gateway(FakeSink(), dispatcher=dispatcher)
    body = {
        "user": {"id": "U2"},
        "actions": [{"type": "button", "action_id": "cruxw0",
                     "value": json.dumps({"token": "TK", "form": "", "value": "Yes"})}],
    }
    await gw._handle_action(body)
    assert ("resolve_pending", "TK", "Yes") in dispatcher.calls
    assert ("consume_action", "TK", "Yes", "U2") in dispatcher.calls


async def test_button_click_resolving_pending_skips_consume() -> None:
    dispatcher = FakeDispatcher(pending_ok=True)  # a blocking request was waiting
    gw = _gateway(FakeSink(), dispatcher=dispatcher)
    body = {
        "user": {"id": "U2"},
        "actions": [{"type": "button", "action_id": "cruxw0",
                     "value": json.dumps({"token": "TK", "form": "", "value": "Allow"})}],
    }
    await gw._handle_action(body)
    assert ("resolve_pending", "TK", "Allow") in dispatcher.calls
    assert not any(c[0] == "consume_action" for c in dispatcher.calls)


async def test_form_open_click_opens_modal() -> None:
    form = SimpleNamespace(agent="assistant", form=Form(title="T", fields=(FormField(name="s", label="S"),)))
    dispatcher = FakeDispatcher(form=form)
    poster = FakePoster()
    gw = _gateway(FakeSink(), dispatcher=dispatcher, poster=poster)
    body = {
        "user": {"id": "U2"},
        "trigger_id": "TRIG",
        "actions": [{"type": "button", "action_id": "cruxw0",
                     "value": json.dumps({"token": "", "form": "F1", "value": ""})}],
    }
    await gw._handle_action(body)
    assert ("load_form", "F1") in dispatcher.calls
    assert poster.opened and poster.opened[0][0] == "TRIG" and poster.opened[0][2] == "F1"


async def test_button_click_strips_the_buttons() -> None:
    # After a fire-and-forget click, Slack won't retire the buttons — the gateway
    # updates the message (blocks dropped) so it can't be clicked again.
    gw = _gateway(FakeSink(), dispatcher=FakeDispatcher())
    updates: list = []

    async def _rec(**kwargs):
        updates.append(kwargs)
        return {"ok": True}

    gw._app.client.chat_update = _rec  # type: ignore[method-assign]
    body = {
        "user": {"id": "U2"},
        "channel": {"id": "C1"},
        "message": {"ts": "9.9"},
        "actions": [{"type": "button", "action_id": "cruxw0",
                     "value": json.dumps({"token": "TK", "form": "", "value": "Yes"})}],
    }
    await gw._handle_action(body)
    assert updates == [{"channel": "C1", "ts": "9.9", "text": "Selected: Yes", "blocks": []}]


async def test_view_submission_feeds_form_values() -> None:
    dispatcher = FakeDispatcher()
    gw = _gateway(FakeSink(), dispatcher=dispatcher)
    body = {
        "user": {"id": "U2"},
        "view": {
            "callback_id": FORM_CALLBACK,
            "private_metadata": "FTOK",
            "state": {"values": {"s": {"s": {"type": "plain_text_input", "value": "hello"}}}},
        },
    }
    await gw._handle_view(body)
    assert ("submit_form", "FTOK", {"s": "hello"}, False, "U2") in dispatcher.calls


# --- message shortcuts (the thread-aware command entry) -----------------------

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
    gw = _gateway(FakeSink(), dispatcher=dispatcher)
    gw._agent = "assistant"

    gw._handle_shortcut(SHORTCUT_IN_THREAD)

    call = dispatcher.calls[0]
    assert call[0] == "invoke_command" and call[1] == "assistant"
    assert call[2] == "C0BC51KQWTX"  # channel
    assert call[3] == "1782309611.465749"  # conversation = the thread root
    assert call[4] == "thread"
    assert call[5] == "/summarize"  # callback id minus the crux_ prefix
    assert call[6] == "U0HNU8P60" and call[7] == "roman.suchkov"


async def test_shortcut_on_a_top_level_message_uses_that_message_as_root() -> None:
    dispatcher = FakeDispatcher()
    gw = _gateway(FakeSink(), dispatcher=dispatcher)
    body = {**SHORTCUT_IN_THREAD, "message": {"ts": "111.2", "text": "top level"}}

    gw._handle_shortcut(body)

    call = dispatcher.calls[0]
    assert call[3] == "111.2" and call[4] == "thread"  # a reply would start this thread


async def test_shortcut_in_a_dm_runs_as_the_dm_conversation() -> None:
    dispatcher = FakeDispatcher()
    gw = _gateway(FakeSink(), dispatcher=dispatcher)
    body = {
        **SHORTCUT_IN_THREAD,
        "channel": {"id": "D0123", "name": "dm"},
        "message": {"ts": "111.2", "text": "hi"},
    }

    gw._handle_shortcut(body)

    call = dispatcher.calls[0]
    assert call[3] == "D0123" and call[4] == "dm"


async def test_shortcut_without_a_command_name_is_ignored() -> None:
    dispatcher = FakeDispatcher()
    gw = _gateway(FakeSink(), dispatcher=dispatcher)

    gw._handle_shortcut({**SHORTCUT_IN_THREAD, "callback_id": "crux_"})

    assert dispatcher.calls == []


async def test_command_prefix_is_configurable() -> None:
    # A workspace with its own shortcut naming: the prefix is config, and the
    # command name is whatever follows it.
    dispatcher = FakeDispatcher()
    gw = _gateway(FakeSink(), dispatcher=dispatcher, command_prefix="acme-")
    body = {**SHORTCUT_IN_THREAD, "callback_id": "acme-summarize"}

    gw._handle_shortcut(body)

    assert dispatcher.calls[0][5] == "/summarize"


async def test_empty_prefix_makes_the_callback_id_the_command() -> None:
    dispatcher = FakeDispatcher()
    gw = _gateway(FakeSink(), dispatcher=dispatcher, command_prefix="")
    body = {**SHORTCUT_IN_THREAD, "callback_id": "summarize"}

    gw._handle_shortcut(body)

    assert dispatcher.calls[0][5] == "/summarize"
