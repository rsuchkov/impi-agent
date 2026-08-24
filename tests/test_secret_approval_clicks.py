"""The click that answers a request for a credential, on both gateways.

Three hops have to agree for an approval to work: the controls the broker builds
must carry the token, each platform's codec must find it again, and the receiver
must route it somewhere that checks who clicked. This file pins all three, and
in particular that a click from the wrong person changes nothing.
"""

import asyncio
from pathlib import Path

import aiohttp

from crucible.approvals import (
    ANSWER_DENY,
    ANSWER_ONCE,
    APPROVAL_KEY,
    ApprovalOutcome,
    PendingApprovals,
    approval_actions,
)
from crucible.gateways.mattermost import MattermostCallbackCodec
from crucible.gateways.slack.rendering import (
    build_action_blocks,
    decode_action,
    decode_approval,
)
from crucible.interactions import (
    InteractionDispatcher,
    InteractionsServer,
    MappingPresence,
)
from crucible.interactions.pending_ui import CONFIRM_YES, PendingUiRequests
from crucible.ports.chat.types import ACTION_SELECT
from crucible.store.sessions import SqliteSessionStore
from tests.fakes.presence import presence_of

APPROVER = "uid-roman"
STRANGER = "uid-someone-else"
WINDOWS = (60, 300)


def _actions(token: str = "tok-1"):
    return approval_actions(token, offers=WINDOWS)


# -- the controls --------------------------------------------------------------


def test_every_control_carries_the_token_it_answers() -> None:
    actions = _actions()
    assert [a.id for a in actions] == ["once", "grant", "deny"]
    assert all(a.context[APPROVAL_KEY] == "tok-1" for a in actions)
    dropdown = next(a for a in actions if a.kind == ACTION_SELECT)
    assert [c.label for c in dropdown.options] == ["1 min", "5 min"]
    assert [c.value for c in dropdown.options] == ["grant:60", "grant:300"]


# -- Mattermost ----------------------------------------------------------------


def test_the_mattermost_codec_finds_the_approval_in_the_context() -> None:
    codec = MattermostCallbackCodec()
    click = codec.parse_action(
        {
            "user_id": APPROVER,
            "context": {APPROVAL_KEY: "tok-1", "value": ANSWER_ONCE},
        }
    )
    assert (click.approval, click.value, click.user_id) == ("tok-1", ANSWER_ONCE, APPROVER)


def test_the_mattermost_codec_finds_a_pick_from_the_dropdown() -> None:
    codec = MattermostCallbackCodec()
    click = codec.parse_action(
        {"user_id": APPROVER, "context": {APPROVAL_KEY: "tok-1", "selected_option": "grant:300"}}
    )
    assert (click.approval, click.value) == ("tok-1", "grant:300")


def test_the_confirm_that_approves_a_tool_call_is_not_this_one() -> None:
    """The confusable case. `ask_user_confirm` and the gate in front of a tool
    call post their own Allow/Block on the same callback — they must stay on the
    ``token`` path, where any click in the conversation answers them."""
    codec = MattermostCallbackCodec()
    click = codec.parse_action(
        {"user_id": STRANGER, "context": {"token": "pending-tok", "value": CONFIRM_YES}}
    )
    assert click.approval == ""
    assert (click.token, click.value) == ("pending-tok", "Allow")


# -- Slack ---------------------------------------------------------------------


def test_slack_round_trips_the_token_through_a_button_and_a_menu() -> None:
    """Slack has no free-form callback context: a button hides it in its value
    and a menu in the block id, so both paths have to be decodable."""
    blocks = build_action_blocks("approve?", _actions())
    elements = [e for b in blocks if b.get("type") == "actions" for e in b["elements"]]
    block_id = next(b["block_id"] for b in blocks if b.get("type") == "actions")

    button = next(e for e in elements if e["type"] == "button")
    assert decode_approval(button) == "tok-1"

    menu = dict(next(e for e in elements if e["type"] == "static_select"))
    menu["block_id"] = block_id  # Slack echoes the containing block's id on a pick
    menu["selected_option"] = {"value": "grant:300"}
    assert decode_approval(menu) == "tok-1"
    assert decode_action(menu)[2] == "grant:300"


def test_a_slack_click_that_is_not_an_approval_decodes_as_none() -> None:
    from crucible.ports.chat.types import Action

    blocks = build_action_blocks(
        "pick", [Action(id="a", label="Yes", value="Yes", context={"token": "t"})]
    )
    button = blocks[-1]["elements"][0]
    assert decode_approval(button) == ""


# -- the dispatcher ------------------------------------------------------------


def _dispatcher(store, approvals: PendingApprovals | None) -> InteractionDispatcher:
    return InteractionDispatcher(
        store,
        presence_of(object()),  # type: ignore[arg-type]  # structural test double
        PendingUiRequests(),
        store,
        approvals=approvals,
    )


async def test_without_a_broker_an_approval_click_is_nobody_s(tmp_path: Path) -> None:
    store = SqliteSessionStore(tmp_path / "db.sqlite")
    try:
        dispatcher = _dispatcher(store, None)
        assert dispatcher.resolve_approval("tok-1", ANSWER_ONCE, APPROVER) is ApprovalOutcome.NOT_MINE
    finally:
        await store.close()


async def test_the_dispatcher_forwards_to_the_registry(tmp_path: Path) -> None:
    store = SqliteSessionStore(tmp_path / "db.sqlite")
    approvals = PendingApprovals()
    try:
        dispatcher = _dispatcher(store, approvals)
        future = approvals.register(
            "tok-1", kind="secret", principal="assistant",
            scopes=("github-token",), approvers=frozenset({APPROVER}),
        )
        assert (
            dispatcher.resolve_approval("tok-1", ANSWER_DENY, STRANGER) is ApprovalOutcome.NOT_ALLOWED
        )
        assert not future.done()
        assert dispatcher.resolve_approval("tok-1", ANSWER_DENY, APPROVER) is ApprovalOutcome.RESOLVED
        assert (await future).allowed is False
    finally:
        await store.close()


# -- the receiver --------------------------------------------------------------


async def _receiver(port: int, store, approvals: PendingApprovals) -> InteractionsServer:
    server = InteractionsServer(
        _dispatcher(store, approvals),
        MattermostCallbackCodec(),
        MappingPresence({}),
        host="127.0.0.1",
        port=port,
    )
    await server.start()
    return server


async def _click(port: int, token: str, value: str, user_id: str) -> dict:
    async with aiohttp.ClientSession() as session:
        async with session.post(
            f"http://127.0.0.1:{port}/interact",
            json={"user_id": user_id, "context": {APPROVAL_KEY: token, "value": value}},
        ) as response:
            assert response.status == 200
            return await response.json()


async def test_an_approver_s_click_answers_and_changes_no_message(tmp_path: Path) -> None:
    """The broker rewrites the card itself once it has the answer, so the
    callback response must leave the message alone rather than race it."""
    store = SqliteSessionStore(tmp_path / "db.sqlite")
    approvals = PendingApprovals()
    server = await _receiver(8511, store, approvals)
    try:
        future = approvals.register(
            "tok-1", kind="secret", principal="assistant",
            scopes=("github-token",), approvers=frozenset({APPROVER}),
        )
        assert await _click(8511, "tok-1", ANSWER_ONCE, APPROVER) == {}
        answer = await asyncio.wait_for(future, timeout=1)
        assert (answer.allowed, answer.grant_s, answer.approver) == (True, 0, APPROVER)
    finally:
        await server.stop()
        await store.close()


async def test_a_stranger_s_click_is_told_off_and_leaves_the_card_live(
    tmp_path: Path,
) -> None:
    store = SqliteSessionStore(tmp_path / "db.sqlite")
    approvals = PendingApprovals()
    server = await _receiver(8512, store, approvals)
    try:
        future = approvals.register(
            "tok-1", kind="secret", principal="assistant",
            scopes=("github-token",), approvers=frozenset({APPROVER}),
        )
        body = await _click(8512, "tok-1", ANSWER_ONCE, STRANGER)
        # Ephemeral: only the person who clicked sees it, and the buttons stay
        # up for the person the request was actually addressed to.
        assert "approver" in body["ephemeral_text"]
        assert not future.done()
        assert approvals.pending("tok-1")
    finally:
        await server.stop()
        await store.close()


async def test_a_click_on_a_dead_request_retires_the_buttons(tmp_path: Path) -> None:
    store = SqliteSessionStore(tmp_path / "db.sqlite")
    server = await _receiver(8513, store, PendingApprovals())
    try:
        body = await _click(8513, "long-gone", ANSWER_ONCE, APPROVER)
        assert "no longer active" in body["update"]["message"]
    finally:
        await server.stop()
        await store.close()


async def test_a_window_picked_from_the_dropdown_reaches_the_broker(
    tmp_path: Path,
) -> None:
    store = SqliteSessionStore(tmp_path / "db.sqlite")
    approvals = PendingApprovals()
    server = await _receiver(8514, store, approvals)
    try:
        future = approvals.register(
            "tok-1", kind="secret", principal="assistant",
            scopes=("github-token",), approvers=frozenset({APPROVER}),
        )
        async with aiohttp.ClientSession() as session:
            async with session.post(
                "http://127.0.0.1:8514/interact",
                json={
                    "user_id": APPROVER,
                    "context": {APPROVAL_KEY: "tok-1", "selected_option": "grant:300"},
                },
            ) as response:
                assert response.status == 200
        answer = await asyncio.wait_for(future, timeout=1)
        assert (answer.allowed, answer.grant_s) == (True, 300)
    finally:
        await server.stop()
        await store.close()
