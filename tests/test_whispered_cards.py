"""A tool's confirmation where other people read (K95): the card goes to the
person who asked, seen by them alone, and what becomes of it afterwards is
what each platform allows — Slack rewrites it through the URL a click offers,
Mattermost cannot rewrite it at all and says the verdict in the click's reply."""

import asyncio
import json
from pathlib import Path

import aiohttp

from crucible.approvals import (
    ANSWER_DENY,
    ANSWER_ONCE,
    APPROVAL_KEY,
    ApprovalOutcome,
    PendingApprovals,
)
from crucible.gateways.mattermost import MattermostCallbackCodec
from crucible.gateways.mattermost.client import MattermostChatClient
from crucible.gateways.slack.client import SlackChatClient
from crucible.gateways.slack.interactions import SlackInteractions
from crucible.interactions import (
    InteractionDispatcher,
    InteractionsServer,
    MappingPresence,
)
from crucible.interactions.callbacks import WHISPERED_KEY
from crucible.interactions.pending_ui import PendingUiRequests
from crucible.ports.chat.client import WhisperedCards
from crucible.ports.chat.types import (
    BUTTONS_RETIRED_TEXT,
    CARD_OVER_TEXT,
    KIND_DM,
    KIND_THREAD,
    Action,
    ApprovalCard,
    ConversationRef,
)
from crucible.store.base import GatewayAddress
from crucible.store.sqlite import SqliteSessionStore
from tests.fakes.dispatch import FakeDispatcher
from tests.fakes.presence import presence_of
from tests.fakes.slack import FakeWeb
from tests.test_tool_gate import CLICKER, FakePoster, Posted, _call, _close, _rig

CARD = ApprovalCard(agent="assistant", headline="assistant wants to run «Delete»", title="Delete", tool="delete")
ACTIONS = [Action(id="once", label="Allow once", value=ANSWER_ONCE, context={APPROVAL_KEY: "tok-1"})]


def _thread(channel: str = "C9", root: str = "1700.1") -> ConversationRef:
    return ConversationRef(channel_id=channel, conversation_id=root, message_id=root, thread_root_id=root)


# -- the gate ------------------------------------------------------------------


class PrivatePoster(FakePoster):
    """A platform that can show a card to one person alone."""

    def __init__(self) -> None:
        super().__init__()
        self.whispered: list[tuple[str, ConversationRef]] = []

    async def whisper_confirmation(self, ref, user_id, card, actions, *, callback_url) -> str:
        self.whispered.append((user_id, ref))
        self.posts.append(Posted("", list(actions), "whispered-1", card, ref))
        return "whispered-1"


async def test_where_other_people_read_the_asker_alone_is_asked(tmp_path: Path) -> None:
    """A thread's conversation: the card is posted to the person whose
    message the turn answers, in the thread, and nobody else sees it; the
    verdict still rewrites it (the platform decides what that does)."""
    poster = PrivatePoster()
    assert isinstance(poster, WhisperedCards)
    rig = await _rig(tmp_path, 8571, by_gateway={"mattermost": poster})
    try:
        await rig.store.set_gateway_address(
            "assistant", "dm1", "mattermost", GatewayAddress("C9", "1700.1", KIND_THREAD)
        )
        await rig.store.get_or_create("assistant", "dm1", "dm1", KIND_DM, user_id=CLICKER, gateway="mattermost")
        pending = asyncio.create_task(_call(rig, "dangerous"))
        await _answer(rig, ANSWER_ONCE, poster)
        assert await pending == 200
        assert [(who, ref.channel_id, ref.thread_root_id) for who, ref in poster.whispered] == [(CLICKER, "C9", "1700.1")]
        assert [u.post_id for u in poster.updated] == ["whispered-1"]
    finally:
        await _close(rig)


async def test_a_direct_message_is_asked_in_the_open_as_before(tmp_path: Path) -> None:
    poster = PrivatePoster()
    rig = await _rig(tmp_path, 8572, by_gateway={"mattermost": poster})
    try:
        await rig.store.set_gateway_address(
            "assistant", "dm1", "mattermost", GatewayAddress("dm1", "dm1", KIND_DM, direct=True)
        )
        await rig.store.get_or_create("assistant", "dm1", "dm1", KIND_DM, user_id=CLICKER, gateway="mattermost")
        pending = asyncio.create_task(_call(rig, "dangerous"))
        await _answer(rig, ANSWER_ONCE, poster)
        assert await pending == 200
        assert poster.whispered == [] and len(poster.posts) == 1
    finally:
        await _close(rig)


async def test_named_approvers_are_asked_in_the_open(tmp_path: Path) -> None:
    """TOOL_APPROVERS: the card is theirs, not the asker's — it stays where
    they can see it."""
    poster = PrivatePoster()
    rig = await _rig(tmp_path, 8573, by_gateway={"mattermost": poster}, approvers={"boss"})
    try:
        await rig.store.set_gateway_address(
            "assistant", "dm1", "mattermost", GatewayAddress("C9", "1700.1", KIND_THREAD)
        )
        await rig.store.get_or_create("assistant", "dm1", "dm1", KIND_DM, user_id=CLICKER, gateway="mattermost")
        pending = asyncio.create_task(_call(rig, "dangerous"))
        token = str((await _wait_posted(poster)).actions[0].context[APPROVAL_KEY])
        assert rig.approvals.resolve(token, ANSWER_ONCE, "boss") is ApprovalOutcome.RESOLVED
        assert await pending == 200
        assert poster.whispered == []
    finally:
        await _close(rig)


async def test_a_copy_on_another_gateway_or_a_platform_without_the_verb_is_posted_as_before(tmp_path: Path) -> None:
    """The person is known on the gateway they spoke through: a copy of the
    card on another gateway cannot be addressed to them there, and a client
    without WhisperedCards is asked the way it always was."""
    slack = PrivatePoster()
    rig = await _rig(tmp_path, 8574, by_gateway={"slack": slack})  # mattermost: the rig's plain FakePoster
    try:
        await rig.store.set_gateway_address(
            "assistant", "dm1", "mattermost", GatewayAddress("C1", "1.1", KIND_THREAD)
        )
        await rig.store.set_gateway_address(
            "assistant", "dm1", "slack", GatewayAddress("C9", "1700.1", KIND_THREAD)
        )
        await rig.store.get_or_create("assistant", "dm1", "dm1", KIND_DM, user_id=CLICKER, gateway="mattermost")
        pending = asyncio.create_task(_call(rig, "dangerous"))
        await _answer(rig, ANSWER_ONCE, rig.poster)
        assert await pending == 200
        assert slack.whispered == [] and len(slack.posts) == 1 and len(rig.poster.posts) == 1
    finally:
        await _close(rig)


async def test_a_platform_that_refuses_the_private_post_is_asked_in_the_open(tmp_path: Path) -> None:
    """A Mattermost bot without the permission to post to one person: the
    question is still asked — in the open, as it always was — rather than
    refused for want of an approver."""

    class Refusing(PrivatePoster):
        async def whisper_confirmation(self, ref, user_id, card, actions, *, callback_url) -> str:
            self.whispered.append((user_id, ref))
            raise RuntimeError("You do not have the appropriate permissions.")

    poster = Refusing()
    rig = await _rig(tmp_path, 8576, by_gateway={"mattermost": poster})
    try:
        await rig.store.set_gateway_address(
            "assistant", "dm1", "mattermost", GatewayAddress("C9", "1700.1", KIND_THREAD)
        )
        await rig.store.get_or_create("assistant", "dm1", "dm1", KIND_DM, user_id=CLICKER, gateway="mattermost")
        pending = asyncio.create_task(_call(rig, "dangerous"))
        await _answer(rig, ANSWER_ONCE, poster)
        assert await pending == 200
        assert len(poster.whispered) == 1 and len(poster.posts) == 1 and poster.posts[0].post_id != "whispered-1"
    finally:
        await _close(rig)


async def _wait_posted(poster: FakePoster) -> Posted:
    for _ in range(400):
        if poster.posts:
            return poster.posts[0]
        await asyncio.sleep(0.005)
    raise AssertionError("nobody was asked")


async def _answer(rig, value: str, poster: FakePoster) -> None:
    token = str((await _wait_posted(poster)).actions[0].context[APPROVAL_KEY])
    assert rig.approvals.resolve(token, value, CLICKER) is ApprovalOutcome.RESOLVED


# -- Slack ---------------------------------------------------------------------


class _Webhook:
    sent: list[dict] = []

    def __init__(self, url: str) -> None:
        self.url = url

    async def send(self, **kwargs):
        _Webhook.sent.append({"url": self.url, **kwargs})
        return None


def _slack(web: FakeWeb) -> SlackChatClient:
    _Webhook.sent = []
    return SlackChatClient(web)  # type: ignore[arg-type]


async def test_slack_posts_the_card_to_the_person_alone_in_the_thread() -> None:
    web = FakeWeb(chat_postEphemeral={"message_ts": "1700.9"}, conversations_replies={"messages": [{"ts": "1700.1"}, {"ts": "1700.2"}]})
    client = _slack(web)
    post_id = await client.whisper_confirmation(_thread(), "U9", CARD, ACTIONS, callback_url="")
    kw = web.last("chat_postEphemeral")
    assert (kw["channel"], kw["user"], kw["thread_ts"]) == ("C9", "U9", "1700.1")
    assert kw["blocks"][-1]["type"] == "actions" and "Delete" in kw["text"]
    assert post_id.startswith("whispered:") and post_id.endswith("1700.9")


async def test_slack_leaves_a_whispered_card_as_it_is() -> None:
    """Slack keeps no such message: chat.update cannot reach it, so the
    verbs that rewrite do nothing — the click is what tells the person."""
    web = FakeWeb(chat_postEphemeral={"message_ts": "1700.9"})
    client = _slack(web)
    post_id = await client.whisper_confirmation(_thread(), "U9", CARD, ACTIONS, callback_url="")
    await client.update_confirmation(post_id, CARD)
    await client.retract(post_id, "expired")
    assert web.all("chat_update") == []


def _ephemeral_click(value: str = ANSWER_ONCE) -> dict:
    return {
        "user": {"id": "U9"}, "channel": {"id": "C9"},
        "container": {"type": "message", "message_ts": "1700.9", "channel_id": "C9", "is_ephemeral": True},
        "response_url": "https://hooks.slack.test/r/2",
        "actions": [{"type": "button", "action_id": "cruxw0",
                     "value": json.dumps({"token": "", "form": "", "value": value, "approval": "APR"})}],
    }


def _interactions(dispatcher) -> SlackInteractions:
    from slack_bolt.async_app import AsyncApp

    _Webhook.sent = []
    app = AsyncApp(token="xoxb-fake", signing_secret="x" * 16)
    return SlackInteractions(app, dispatcher, _slack(FakeWeb()), agent="assistant", webhooks=_Webhook)  # type: ignore[arg-type]


async def test_a_click_on_a_whispered_card_replaces_it_with_the_verdict() -> None:
    """The broker's rewrite cannot reach an ephemeral message, and it runs
    wherever the turn runs — on another replica, maybe. The click's own URL
    is the one way to the card, so the click handler is what tells the
    person, in the card's place."""
    dispatcher = FakeDispatcher(approval=ApprovalOutcome.RESOLVED)
    it = _interactions(dispatcher)
    await it.handle_action(_ephemeral_click())
    assert ("resolve_approval", "APR", "once", "U9") in dispatcher.calls
    assert len(_Webhook.sent) == 1
    sent = _Webhook.sent[0]
    assert sent["url"] == "https://hooks.slack.test/r/2" and sent["replace_original"] is True
    assert sent["blocks"] == [] and sent["text"] == "Allowed once"
    await it.handle_action(_ephemeral_click("grant:300"))
    assert _Webhook.sent[-1]["text"] == "Allowed for 5 min"
    await it.handle_action(_ephemeral_click(ANSWER_DENY))
    assert _Webhook.sent[-1]["text"] == "Denied"


async def test_a_click_on_a_whispered_card_whose_question_is_over_says_so_in_its_place() -> None:
    """Expired, withdrawn, answered on another copy: the card could not be
    retired, so the click is what tells the person — and what to do."""
    it = _interactions(FakeDispatcher(approval=ApprovalOutcome.NOT_MINE))
    await it.handle_action(_ephemeral_click())
    assert len(_Webhook.sent) == 1
    sent = _Webhook.sent[0]
    assert sent["url"] == "https://hooks.slack.test/r/2" and sent["replace_original"] is True
    assert sent["blocks"] == [] and sent["text"] == CARD_OVER_TEXT


async def test_a_stranger_s_click_on_a_whispered_card_changes_nothing() -> None:
    it = _interactions(FakeDispatcher(approval=ApprovalOutcome.NOT_ALLOWED))
    whispers: list[dict] = []

    async def _rec(**kwargs):
        whispers.append(kwargs)
        return {"ok": True}

    it._app.client.chat_postEphemeral = _rec  # type: ignore[method-assign]
    await it.handle_action(_ephemeral_click())
    assert _Webhook.sent == [] and whispers and whispers[0]["user"] == "U9"


async def test_a_click_on_a_card_in_the_open_is_handled_as_before() -> None:
    dispatcher = FakeDispatcher(approval=ApprovalOutcome.NOT_MINE)
    it = _interactions(dispatcher)
    updates: list[dict] = []

    async def _rec(**kwargs):
        updates.append(kwargs)
        return {"ok": True}

    it._app.client.chat_update = _rec  # type: ignore[method-assign]
    click = {**_ephemeral_click(), "message": {"ts": "5.5"}, "container": {"message_ts": "5.5", "channel_id": "C9"}}
    await it.handle_action(click)
    assert updates and updates[0]["text"] == BUTTONS_RETIRED_TEXT and _Webhook.sent == []
    it = _interactions(FakeDispatcher(approval=ApprovalOutcome.RESOLVED))
    it._app.client.chat_update = _rec  # type: ignore[method-assign]
    await it.handle_action(click)
    assert len(updates) == 1 and _Webhook.sent == []  # the broker rewrites its own card


# -- Mattermost ----------------------------------------------------------------


class _Driver:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.client = type("C", (), {"userid": "botid"})()
        driver = self

        class Posts:
            async def create_post_ephemeral(self, user_id, post):
                driver.calls.append(("create_post_ephemeral", {"user_id": user_id, "post": post}))
                return {"id": "eph-1"}

            async def patch_post(self, post_id, **options):
                driver.calls.append(("patch_post", {"post_id": post_id, **options}))
                return {"id": post_id}

        self.posts = Posts()


async def test_mattermost_posts_the_card_to_the_person_alone_with_its_actions_marked_whispered() -> None:
    driver = _Driver()
    client = MattermostChatClient(driver)  # type: ignore[arg-type]
    post_id = await client.whisper_confirmation(_thread(), "u9", CARD, ACTIONS, callback_url="http://engine/i")
    name, kw = driver.calls[0]
    assert name == "create_post_ephemeral" and kw["user_id"] == "u9"
    post = kw["post"]
    assert (post["channel_id"], post["root_id"]) == ("C9", "1700.1") and "Delete" in post["message"]
    action = post["props"]["attachments"][0]["actions"][0]
    assert action["integration"]["context"][WHISPERED_KEY] and action["integration"]["context"][APPROVAL_KEY] == "tok-1"
    assert ACTIONS[0].context == {APPROVAL_KEY: "tok-1"}  # the caller's actions are not changed under it
    # Kept by nobody: a rewrite by its id is not attempted (the API refuses it).
    await client.update_confirmation(post_id, CARD)
    await client.retract(post_id, "expired")
    assert [n for n, _ in driver.calls] == ["create_post_ephemeral"]


def test_the_mattermost_codec_reads_the_whispered_mark() -> None:
    codec = MattermostCallbackCodec()
    click = codec.parse_action({"user_id": "u9", "context": {APPROVAL_KEY: "tok-1", "value": ANSWER_ONCE, WHISPERED_KEY: "1"}})
    assert click.whispered is True and click.approval == "tok-1"
    assert codec.parse_action({"user_id": "u9", "context": {APPROVAL_KEY: "tok-1"}}).whispered is False


async def _receiver(port: int, store, approvals: PendingApprovals) -> InteractionsServer:
    dispatcher = InteractionDispatcher(
        store, presence_of(object()), PendingUiRequests(), store, approvals=approvals,  # type: ignore[arg-type]
    )
    server = InteractionsServer(dispatcher, MattermostCallbackCodec(), MappingPresence({}), host="127.0.0.1", port=port)
    await server.start()
    return server


async def _click(port: int, context: dict, user_id: str = "u9") -> dict:
    async with aiohttp.ClientSession() as session:
        async with session.post(f"http://127.0.0.1:{port}/interact", json={"user_id": user_id, "context": context}) as response:
            assert response.status == 200
            return await response.json()


async def test_the_receiver_tells_the_verdict_of_a_whispered_card_in_the_click_s_reply(tmp_path: Path) -> None:
    """The card cannot be rewritten, so the reply is what says it — a line
    the clicker alone sees; a card in the open is left to its owner as before."""
    store = SqliteSessionStore(tmp_path / "db.sqlite")
    approvals = PendingApprovals()
    server = await _receiver(8575, store, approvals)
    try:
        future = approvals.register("tok-1", kind="tool", principal="assistant", scopes=("delete",), approvers=frozenset({"u9"}))
        assert await _click(8575, {APPROVAL_KEY: "tok-1", "value": ANSWER_ONCE, WHISPERED_KEY: "1"}) == {"ephemeral_text": "Allowed once"}
        assert (await asyncio.wait_for(future, timeout=1)).allowed is True
        future = approvals.register("tok-2", kind="tool", principal="assistant", scopes=("delete",), approvers=frozenset({"u9"}))
        assert await _click(8575, {APPROVAL_KEY: "tok-2", "selected_option": "grant:300", WHISPERED_KEY: "1"}) == {"ephemeral_text": "Allowed for 5 min"}
        future = approvals.register("tok-3", kind="tool", principal="assistant", scopes=("delete",), approvers=frozenset({"u9"}))
        assert await _click(8575, {APPROVAL_KEY: "tok-3", "value": ANSWER_DENY, WHISPERED_KEY: "1"}) == {"ephemeral_text": "Denied"}
        future = approvals.register("tok-4", kind="tool", principal="assistant", scopes=("delete",), approvers=frozenset({"u9"}))
        assert await _click(8575, {APPROVAL_KEY: "tok-4", "value": ANSWER_ONCE}) == {}
        # A click on a whispered card whose question is over: told so, in place
        # of the rewrite a card in the open gets.
        assert await _click(8575, {APPROVAL_KEY: "tok-1", "value": ANSWER_ONCE, WHISPERED_KEY: "1"}) == {"ephemeral_text": CARD_OVER_TEXT}
        assert await _click(8575, {APPROVAL_KEY: "tok-1", "value": ANSWER_ONCE}) == {"update": {"message": BUTTONS_RETIRED_TEXT, "props": {"attachments": []}}}
    finally:
        await server.stop()
        await store.close()


# -- a trusted agent's turn (K98) --------------------------------------------------


async def test_a_trusted_agents_card_where_other_people_read_is_whispered_through_the_askers_client(tmp_path: Path) -> None:
    """The door is the task's gateway; it reaches the person through the
    asker's own client on the gateway they spoke through. Where that
    conversation is read by others (the scope says so — the task's own
    conversation is nobody's and would say "direct" of every card), the card
    is whispered there and copied in the open on the asker's other gateways."""
    from crucible.gateways.delegation import TrustedDelegationChatClient
    from crucible.interactions.delegation import TrustedLinkRegistry
    from crucible.ports.delegation import (
        ON_BEHALF_OF_DIRECT_ATTRIBUTE,
        ON_BEHALF_OF_GATEWAY_ATTRIBUTE,
        TrustedLink,
    )
    from crucible.ports.turn import TurnScope
    from crucible.turns import TurnRegistry

    links = TrustedLinkRegistry()
    slack, panel = PrivatePoster(), PrivatePoster()
    links.register("dm1", TrustedLink(
        asker="assistant", asker_session_id="assistant--asker", to="jira", poster=slack, ref=_thread(),  # type: ignore[arg-type]
        elsewhere=((panel, _thread("issue-7", "")),),  # type: ignore[arg-type]
    ))
    turns = TurnRegistry()
    rig = await _rig(tmp_path, 8577, by_gateway={"trusted-delegation": TrustedDelegationChatClient(links, keeps=False)}, turns=turns)
    try:
        await rig.store.get_or_create("assistant", "dm1", "dm1", KIND_DM, user_id=CLICKER, gateway="trusted-delegation")
        scope = TurnScope(turn_id="t", attributes={ON_BEHALF_OF_GATEWAY_ATTRIBUTE: "slack", ON_BEHALF_OF_DIRECT_ATTRIBUTE: "0"})
        async with turns.bind(rig.session, scope):
            pending = asyncio.create_task(_call(rig, "dangerous"))
            await _answer(rig, ANSWER_ONCE, slack)
            assert await pending == 200
        assert [(who, ref.channel_id) for who, ref in slack.whispered] == [(CLICKER, "C9")]
        assert panel.whispered == [] and len(panel.posts) == 1  # the copy elsewhere, in the open
        assert len(slack.updated) == 1 and len(panel.updated) == 1
    finally:
        await _close(rig)


async def test_a_trusted_agents_card_in_the_askers_direct_message_is_asked_in_the_open(tmp_path: Path) -> None:
    from crucible.gateways.delegation import TrustedDelegationChatClient
    from crucible.interactions.delegation import TrustedLinkRegistry
    from crucible.ports.delegation import (
        ON_BEHALF_OF_DIRECT_ATTRIBUTE,
        ON_BEHALF_OF_GATEWAY_ATTRIBUTE,
        TrustedLink,
    )
    from crucible.ports.turn import TurnScope
    from crucible.turns import TurnRegistry

    links = TrustedLinkRegistry()
    slack = PrivatePoster()
    links.register("dm1", TrustedLink(asker="assistant", asker_session_id="assistant--asker", to="jira", poster=slack, ref=_thread("D1")))  # type: ignore[arg-type]
    turns = TurnRegistry()
    rig = await _rig(tmp_path, 8578, by_gateway={"trusted-delegation": TrustedDelegationChatClient(links, keeps=False)}, turns=turns)
    try:
        await rig.store.get_or_create("assistant", "dm1", "dm1", KIND_DM, user_id=CLICKER, gateway="trusted-delegation")
        scope = TurnScope(turn_id="t", attributes={ON_BEHALF_OF_GATEWAY_ATTRIBUTE: "slack", ON_BEHALF_OF_DIRECT_ATTRIBUTE: "1"})
        async with turns.bind(rig.session, scope):
            pending = asyncio.create_task(_call(rig, "dangerous"))
            await _answer(rig, ANSWER_ONCE, slack)
            assert await pending == 200
        assert slack.whispered == [] and len(slack.posts) == 1
    finally:
        await _close(rig)


async def test_the_door_asks_in_the_open_where_the_askers_client_cannot_whisper(tmp_path: Path) -> None:
    from crucible.gateways.delegation import TrustedDelegationChatClient
    from crucible.interactions.delegation import TrustedLinkRegistry
    from crucible.ports.delegation import (
        ON_BEHALF_OF_DIRECT_ATTRIBUTE,
        ON_BEHALF_OF_GATEWAY_ATTRIBUTE,
        TrustedLink,
    )
    from crucible.ports.turn import TurnScope
    from crucible.turns import TurnRegistry

    links = TrustedLinkRegistry()
    plain = FakePoster()  # no WhisperedCards
    links.register("dm1", TrustedLink(asker="assistant", asker_session_id="assistant--asker", to="jira", poster=plain, ref=_thread()))  # type: ignore[arg-type]
    turns = TurnRegistry()
    rig = await _rig(tmp_path, 8579, by_gateway={"trusted-delegation": TrustedDelegationChatClient(links, keeps=False)}, turns=turns)
    try:
        await rig.store.get_or_create("assistant", "dm1", "dm1", KIND_DM, user_id=CLICKER, gateway="trusted-delegation")
        scope = TurnScope(turn_id="t", attributes={ON_BEHALF_OF_GATEWAY_ATTRIBUTE: "slack", ON_BEHALF_OF_DIRECT_ATTRIBUTE: "0"})
        async with turns.bind(rig.session, scope):
            pending = asyncio.create_task(_call(rig, "dangerous"))
            await _answer(rig, ANSWER_ONCE, plain)
            assert await pending == 200
        assert len(plain.posts) == 1
    finally:
        await _close(rig)
