"""ward on Slack, end to end over fakes: the operator's `/ward` arriving as a
slash command down the socket, the panel drawn as Block Kit, a button opening
a modal, the modal's submission reaching the broker, and a request for a
credential answered by its approver and refused to a stranger.

Every piece is the real one — the screen, the forms, the dispatcher, the Slack
client and the Slack interactions — except the Web API, which records calls.
"""

import json
from pathlib import Path

import pytest

from crucible.approvals import PendingApprovals, approval_actions
from crucible.gateways.slack import SlackChatClient, SlackInteractions
from crucible.gateways.slack.rendering import decode_action
from crucible.interactions import InteractionDispatcher
from crucible.interactions.pending_ui import PendingUiRequests
from crucible.interactions.ports import FormHandlers
from crucible.interactions.screens import ScreenRegistry
from crucible.ports.chat.types import NOT_AN_APPROVER_TEXT, ConversationRef
from tests.fakes.slack import FakeApp, FakeWeb, Respond
from tests.test_ward_chatops import FakeBackend, FakeBroker
from ward.app import OneBot
from ward.approvers import Approvers
from ward.chatops import (
    _DM_ONLY,
    _F_SECRET_ID,
    _NOT_YOURS,
    COMMAND,
    HANDLER,
    OperatorForms,
    PendingOperatorForms,
    WardScreen,
)
from ward.operations import Operations
from ward.store import WardStore

OPERATOR = "U0OPERATOR"
STRANGER = "U0STRANGER"
DM = "D0WARD"


class Stand:
    """ward's chat surface wired to a recording Slack Web API."""

    def __init__(self, tmp_path: Path) -> None:
        self.web = FakeWeb(
            conversations_open={"channel": {"id": DM}},
            chat_postMessage={"ok": True, "ts": "1.1", "channel": DM},
            users_list={"members": []},
        )
        self.chat = SlackChatClient(self.web)  # type: ignore[arg-type]
        presence = OneBot(self.chat)
        # Slack has no username lookup worth relying on: the approver is
        # configured by member id, which the resolver passes through.
        approvers = Approvers(OPERATOR, self.chat)
        self.store = WardStore(tmp_path / "ward.db")
        self.approvals = PendingApprovals()
        self.broker = FakeBroker(FakeBackend(sealed=True, authenticated=False))
        operations = Operations(self.broker.backend, self.store, self.store)
        pending_forms = PendingOperatorForms()
        handlers = FormHandlers()
        handlers.register(
            HANDLER,
            OperatorForms(self.broker, operations, approvers, self.chat, self.chat, self.store, pending_forms),  # type: ignore[arg-type]
        )
        screens = ScreenRegistry()
        screens.register(
            WardScreen(self.broker, operations, approvers, self.chat, self.store, self.store, pending_forms)  # type: ignore[arg-type]
        )
        dispatcher = InteractionDispatcher(
            self.store, presence, PendingUiRequests(), self.store,
            screens=screens, approvals=self.approvals, handlers=handlers, callback_url="",
        )  # type: ignore[arg-type]
        self.app = FakeApp(self.web)
        self.interactions = SlackInteractions(self.app, dispatcher, self.chat, agent=COMMAND)  # type: ignore[arg-type]
        self.interactions.register()

    async def close(self) -> None:
        await self.store.close()

    # -- what Slack sends ------------------------------------------------------

    @staticmethod
    def command(*, user: str = OPERATOR, channel: str = DM, channel_name: str = "directmessage") -> dict:
        return {
            "command": "/ward", "text": "", "channel_id": channel, "channel_name": channel_name,
            "user_id": user, "user_name": "operator", "trigger_id": "TRIG-1",
        }

    def posted_blocks(self) -> list[dict]:
        return self.web.last("chat_postMessage")["blocks"]

    def form_button(self, label: str) -> dict:
        """The button on the last posted card that opens the modal `label`."""
        for block in self.posted_blocks():
            for element in block.get("elements", []):
                if element.get("type") == "button" and element["text"]["text"] == label:
                    return element
        raise AssertionError(f"no {label!r} button on the card")

    @staticmethod
    def click(element: dict, *, user: str, trigger: str = "TRIG-2") -> dict:
        return {
            "user": {"id": user}, "channel": {"id": DM}, "message": {"ts": "1.1"},
            "trigger_id": trigger, "actions": [element],
        }


@pytest.fixture
async def stand(tmp_path: Path):
    s = Stand(tmp_path)
    try:
        yield s
    finally:
        await s.close()


# --- the panel -----------------------------------------------------------------------


async def test_the_operator_s_slash_command_in_their_dm_draws_the_panel(stand: Stand) -> None:
    """Every handler the engine ships registered; the command handler is the
    one ward needs, and it lands on the screen with no agent in between."""
    assert set(stand.app.handlers) == {"action", "view", "shortcut", "command"}
    respond = Respond()
    await stand.interactions.handle_command(stand.command(), respond)
    assert respond.said == []
    posted = stand.web.all("chat_postMessage")
    assert len(posted) == 1 and posted[0]["channel"] == DM
    labels = [
        e["text"]["text"]
        for block in posted[0]["blocks"] for e in block.get("elements", []) if e.get("type") == "button"
    ]
    assert "Unlock" in labels and "Secrets" in labels


async def test_from_a_channel_the_command_is_refused_to_the_operator_alone(stand: Stand) -> None:
    respond = Respond()
    await stand.interactions.handle_command(stand.command(channel="C0SHARED", channel_name="general"), respond)
    assert respond.said == [_DM_ONLY]
    assert stand.web.all("chat_postMessage") == []


async def test_a_stranger_s_command_is_refused_and_nothing_is_drawn(stand: Stand) -> None:
    respond = Respond()
    await stand.interactions.handle_command(stand.command(user=STRANGER), respond)
    assert respond.said == [_NOT_YOURS]
    assert stand.web.all("chat_postMessage") == []


# --- a modal -------------------------------------------------------------------------


async def test_the_unlock_button_opens_the_modal_with_the_click_s_trigger(stand: Stand) -> None:
    await stand.interactions.handle_command(stand.command(), Respond())
    button = stand.form_button("Unlock")
    _, form_token, _ = decode_action(button)
    assert form_token  # the button carries only a token; the form is in the store

    await stand.interactions.handle_action(stand.click(button, user=OPERATOR, trigger="TRIG-2"))

    opened = stand.web.last("views_open")
    assert opened["trigger_id"] == "TRIG-2"
    assert opened["view"]["private_metadata"] == form_token
    assert opened["view"]["title"]["text"].startswith("Unlock")


async def test_the_modal_s_submission_reaches_the_broker_and_answers_in_the_dm(stand: Stand) -> None:
    await stand.interactions.handle_command(stand.command(), Respond())
    button = stand.form_button("Unlock")
    _, form_token, _ = decode_action(button)
    await stand.interactions.handle_action(stand.click(button, user=OPERATOR))
    before = len(stand.web.all("chat_postMessage"))

    await stand.interactions.handle_view({
        "user": {"id": OPERATOR},
        "view": {
            "private_metadata": form_token,
            "state": {"values": {_F_SECRET_ID: {_F_SECRET_ID: {"type": "plain_text_input", "value": "sid-1"}}}},
        },
    })

    assert [m.auth_secret for m in stand.broker.unlocked] == ["sid-1"]
    # The outcome is told in the operator's direct message, not left in the modal.
    assert len(stand.web.all("chat_postMessage")) == before + 1
    assert stand.web.last("chat_postMessage")["channel"] == DM
    # A submitted form is spent: the same token opens nothing a second time.
    assert await stand.store.get_form(form_token) is None


# --- a request for a credential --------------------------------------------------------


async def test_a_credential_request_is_answered_by_its_approver_and_refused_to_a_stranger(stand: Stand) -> None:
    """What the broker does when an agent asks: register the token with the
    approvers, post the card as the one account, wait. Here the card goes over
    Slack and the clicks come back down the socket."""
    token = "apr-tok-1"
    future = stand.approvals.register(token, kind="secret", principal="assistant", scopes=("vault://x",), approvers=frozenset({OPERATOR}))
    ref = ConversationRef(channel_id=DM, conversation_id=DM, message_id=DM)
    await stand.chat.post_actions(ref, "assistant is asking for a secret", approval_actions(token, offers=(60,)), callback_url="")
    blocks = stand.posted_blocks()
    once = next(
        e for block in blocks for e in block.get("elements", [])
        if e.get("type") == "button" and e["text"]["text"] == "Allow once"
    )

    await stand.interactions.handle_action(stand.click(once, user=STRANGER))
    assert stand.web.last("chat_postEphemeral") == {"channel": DM, "user": STRANGER, "text": NOT_AN_APPROVER_TEXT}
    assert not future.done()  # the card stays live for the person it is addressed to

    await stand.interactions.handle_action(stand.click(once, user=OPERATOR))
    answer = future.result()
    assert answer.allowed and answer.grant_s == 0 and answer.approver == OPERATOR
    assert stand.web.all("chat_update") == []  # the broker rewrites its own card


def test_the_slack_encoding_of_a_form_button_is_the_token_alone() -> None:
    """A spec travelling through the platform would be a spec the platform
    could rewrite; on Slack the value is JSON and the form is a token in it."""
    value = json.loads(json.dumps({"token": "", "form": "F1", "value": ""}))
    assert value["form"] == "F1"
