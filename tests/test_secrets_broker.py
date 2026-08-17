"""The broker end to end (crucible/secrets/broker.py).

A real store, a fake backend and a fake poster: the logic under test is the
order of the checks and what each outcome leaves behind, none of which needs a
live Vault or a live Mattermost.

Two properties get the most attention, because they are the ones a reader has to
trust rather than read: a refused caller learns nothing about why, and a click
from the wrong person decides nothing.
"""

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from crucible.ports.chat.types import ACTION_SELECT, Action, ConversationRef
from crucible.secrets.approvals import (
    ANSWER_DENY,
    ANSWER_ONCE,
    SECRET_APPROVAL_KEY,
    SecretApprovalOutcome,
    SecretApprovals,
)
from crucible.secrets.broker import SecretBroker
from crucible.secrets.ports import (
    WIRE_REFUSED,
    WIRE_UNAVAILABLE,
    BackendStatus,
    LeaseRequest,
    SecretBackendError,
    SecretRef,
    UnlockMaterial,
    parse_ref,
    wire_status,
)
from crucible.store.base import (
    APPROVAL_ALWAYS,
    APPROVAL_NEVER,
    DECISION_APPROVED_GRANT,
    DECISION_APPROVED_ONCE,
    DECISION_AUTO,
    DECISION_BACKEND_ERROR,
    DECISION_DENIED,
    DECISION_LOCKED,
    DECISION_NO_APPROVER,
    DECISION_NO_POLICY,
    DECISION_NOT_PERMITTED,
    DECISION_REUSED_GRANT,
    DECISION_SEALED,
    DECISION_TIMEOUT,
    KIND_SECRET,
    SecretPolicyRecord,
)
from crucible.store.sessions import SqliteSessionStore

T0 = "2026-08-11T09:00:00+00:00"
APPROVER = "u1"
STRANGER = "u2"


# -- fakes ---------------------------------------------------------------------


class FakeBackend:
    def __init__(self, values: dict[str, dict[str, str]] | None = None, **state) -> None:
        self.values = values if values is not None else {"github-token": {"value": "ghp_x"}}
        self._state = BackendStatus(
            reachable=state.get("reachable", True),
            sealed=state.get("sealed", False),
            authenticated=state.get("authenticated", True),
        )
        self.reads: list[SecretRef] = []
        self.boom = False

    async def status(self) -> BackendStatus:
        return self._state

    async def unlock(self, material: UnlockMaterial) -> BackendStatus:
        self._state = BackendStatus(reachable=True, sealed=False, authenticated=True)
        return self._state

    async def read(self, ref: SecretRef) -> str:
        self.reads.append(ref)
        if self.boom:
            raise SecretBackendError("the backend fell over")
        entry = self.values.get(ref.name)
        if entry is None or ref.field not in entry:
            raise SecretBackendError(f"no secret named {ref.name}")
        return entry[ref.field]

    async def write(self, name: str, values: Mapping[str, str]) -> None:
        self.values[name] = dict(values)

    async def delete(self, name: str) -> None:
        self.values.pop(name, None)

    async def names(self) -> list[str]:
        return sorted(self.values)

    async def close(self) -> None:
        return None


@dataclass
class Posted:
    ref: ConversationRef
    text: str
    actions: list[Action]
    post_id: str


class FakePoster:
    def __init__(self) -> None:
        self.posts: list[Posted] = []
        self.retracted: list[tuple[str, str]] = []

    async def post_actions(self, ref, text, actions, *, callback_url) -> str:
        post_id = f"post-{len(self.posts) + 1}"
        self.posts.append(Posted(ref, text, list(actions), post_id))
        return post_id

    async def retract(self, post_id: str, text: str) -> None:
        self.retracted.append((post_id, text))


@dataclass
class FakePresence:
    chat: FakePoster | None

    def poster(self, agent: str):
        return self.chat

    def sink(self, agent: str):
        return None


@dataclass
class FakeAdmin:
    """Only the two verbs the broker uses; ``roman`` is a known handle."""

    opened: list[str] = field(default_factory=list)
    directs: bool = True

    async def resolve_username(self, username: str) -> str | None:
        return APPROVER if username == "roman" else None

    async def open_direct(self, user_id: str) -> str:
        self.opened.append(user_id)
        return f"dm-{user_id}" if self.directs else ""


# -- harness -------------------------------------------------------------------


@dataclass
class Rig:
    broker: SecretBroker
    store: SqliteSessionStore
    backend: FakeBackend
    poster: FakePoster
    admin: FakeAdmin
    approvals: SecretApprovals


async def _rig(tmp_path: Path, *, backend: FakeBackend | None = None, **over) -> Rig:
    store = SqliteSessionStore(tmp_path / "db.sqlite")
    backend = backend or FakeBackend()
    poster = FakePoster()
    admin = FakeAdmin()
    approvals = SecretApprovals()
    broker = SecretBroker(
        backend, store, store,  # policies, then the shared window/ledger store
        FakePresence(poster), {"assistant": admin},  # type: ignore[arg-type]
        approvals,
        approvers=over.pop("approvers", "roman"),
        approval_timeout_s=over.pop("approval_timeout_s", 5.0),
        max_grant_s=over.pop("max_grant_s", 3600),
        callback_url="http://engine/interact",
        **over,
    )
    return Rig(broker, store, backend, poster, admin, approvals)


def _policy(**over) -> SecretPolicyRecord:
    base = dict(
        name="github-token", approval=APPROVAL_ALWAYS, max_grant_s=3600,
        subjects="assistant", description="", created_at=T0, updated_at=T0,
    )
    base.update(over)
    return SecretPolicyRecord(**base)  # type: ignore[arg-type]


def _request(ref: str = "vault://github-token", **over) -> LeaseRequest:
    base = dict(
        agent="assistant", runtime_session_id="assistant--dm1",
        bindings=((over.pop("env", "GITHUB_TOKEN"), parse_ref(ref)),),
        reason="push release", command=("gh", "release", "create", "v1.2.0"),
    )
    base.update(over)
    return LeaseRequest(**base)  # type: ignore[arg-type]


async def _card(rig: Rig) -> Posted:
    """Wait for a card that is still waiting on an answer.

    Identified by its token still being pending rather than by being the last
    one posted: a test that leases twice has an older, already-retired card
    sitting in the poster's log, and answering that one would prove nothing.
    """
    for _ in range(400):
        for card in reversed(rig.poster.posts):
            token = str(card.actions[0].context.get(SECRET_APPROVAL_KEY, ""))
            if token and rig.approvals.pending(token):
                return card
        await asyncio.sleep(0.005)
    raise AssertionError("no approval card is waiting for an answer")


def _token(card: Posted) -> str:
    return str(card.actions[0].context[SECRET_APPROVAL_KEY])


async def _answer(rig: Rig, value: str, *, user: str = APPROVER) -> Posted:
    card = await _card(rig)
    assert rig.approvals.resolve(_token(card), value, user) is SecretApprovalOutcome.RESOLVED
    return card


# -- serving -------------------------------------------------------------------


async def test_an_approved_request_gets_the_value_and_leaves_a_row(tmp_path: Path) -> None:
    rig = await _rig(tmp_path)
    try:
        await rig.store.put_policy(_policy())
        pending = asyncio.create_task(rig.broker.lease(_request()))
        await _answer(rig, ANSWER_ONCE)
        result = await pending

        assert result.granted and result.values == {"GITHUB_TOKEN": "ghp_x"}
        row = (await rig.store.list_audit())[0]
        assert (row.decision, row.approver, row.grant_id) == (
            DECISION_APPROVED_ONCE, APPROVER, "",
        )
        assert row.detail == "gh release create v1.2.0"
        # Allowing once leaves nothing behind: the next call asks again.
        assert await rig.store.list_grants(now=T0, kind=KIND_SECRET) == []
    finally:
        await rig.store.close()


async def test_the_card_shows_the_agent_the_reference_and_the_exact_command(
    tmp_path: Path,
) -> None:
    """The argv is the only thing between an approval and an exfiltration: a
    caller may legally ask for a secret in order to echo it."""
    rig = await _rig(tmp_path)
    try:
        await rig.store.put_policy(_policy())
        pending = asyncio.create_task(
            rig.broker.lease(_request(command=("sh", "-c", "echo $GITHUB_TOKEN")))
        )
        card = await _answer(rig, ANSWER_DENY)
        await pending

        assert "assistant" in card.text
        assert "vault://github-token" in card.text
        assert "push release" in card.text
        # Quoted per argument, so the card cannot hide where one ends.
        assert "sh -c 'echo $GITHUB_TOKEN'" in card.text
    finally:
        await rig.store.close()


async def test_several_fields_of_one_secret_are_one_question(tmp_path: Path) -> None:
    rig = await _rig(
        tmp_path, backend=FakeBackend({"smtp": {"username": "bot", "password": "hunter2"}})
    )
    try:
        await rig.store.put_policy(_policy(name="smtp"))
        request = LeaseRequest(
            agent="assistant", runtime_session_id="s",
            bindings=(
                ("SMTP_USER", parse_ref("vault://smtp#username")),
                ("SMTP_PASS", parse_ref("vault://smtp#password")),
            ),
            reason="send the digest", command=("mailer",),
        )
        pending = asyncio.create_task(rig.broker.lease(request))
        await _answer(rig, ANSWER_ONCE)
        result = await pending

        assert result.values == {"SMTP_USER": "bot", "SMTP_PASS": "hunter2"}
        assert len(rig.poster.posts) == 1  # asked once, not once per field
        assert len(await rig.store.list_audit()) == 1
    finally:
        await rig.store.close()


async def test_two_different_secrets_in_one_call_are_malformed(tmp_path: Path) -> None:
    """Not a refusal — a request the caller built wrong. Two secrets nest as two
    invocations, so each is approved on its own terms."""
    rig = await _rig(tmp_path)
    try:
        request = LeaseRequest(
            agent="assistant", runtime_session_id="s",
            bindings=(("A", parse_ref("vault://a")), ("B", parse_ref("vault://b"))),
        )
        with pytest.raises(ValueError):
            await rig.broker.lease(request)
        assert await rig.store.list_audit() == []
    finally:
        await rig.store.close()


async def test_a_policy_that_needs_no_human_serves_straight_away(tmp_path: Path) -> None:
    rig = await _rig(tmp_path)
    try:
        await rig.store.put_policy(_policy(approval=APPROVAL_NEVER))
        result = await rig.broker.lease(_request())
        assert result.granted
        assert rig.poster.posts == []  # nobody was disturbed
        assert (await rig.store.list_audit())[0].decision == DECISION_AUTO
    finally:
        await rig.store.close()


# -- windows -------------------------------------------------------------------


async def test_a_window_is_opened_and_then_reused_without_asking(tmp_path: Path) -> None:
    rig = await _rig(tmp_path)
    try:
        await rig.store.put_policy(_policy())
        pending = asyncio.create_task(rig.broker.lease(_request()))
        await _answer(rig, "grant:300")
        first = await pending
        assert first.granted and first.decision == DECISION_APPROVED_GRANT

        second = await rig.broker.lease(_request())
        assert second.granted and second.values == {"GITHUB_TOKEN": "ghp_x"}
        assert len(rig.poster.posts) == 1  # asked once, served twice
        assert [row.decision for row in await rig.store.list_audit()] == [
            DECISION_REUSED_GRANT, DECISION_APPROVED_GRANT,
        ]
        grants = await rig.store.list_grants(now=T0, kind=KIND_SECRET)
        assert len(grants) == 1 and grants[0].granted_by == APPROVER
        # Both rows point at the window that served them.
        assert {row.grant_id for row in await rig.store.list_audit()} == {grants[0].id}
    finally:
        await rig.store.close()


async def test_a_window_is_capped_by_the_policy_however_it_was_asked_for(
    tmp_path: Path,
) -> None:
    """The ladder is filtered before it is shown, but a hand-made callback can
    send anything, so the ceiling is applied again when the window is opened."""
    rig = await _rig(tmp_path)
    try:
        await rig.store.put_policy(_policy(max_grant_s=300))
        pending = asyncio.create_task(rig.broker.lease(_request()))
        card = await _card(rig)
        dropdown = next(a for a in card.actions if a.kind == ACTION_SELECT)
        assert [c.value for c in dropdown.options] == ["grant:60", "grant:300"]
        assert rig.approvals.resolve(_token(card), "grant:86400", APPROVER) is SecretApprovalOutcome.RESOLVED
        await pending

        grant = (await rig.store.list_grants(now=T0, kind=KIND_SECRET))[0]
        assert grant.expires_at <= _plus(grant.granted_at, 300)
    finally:
        await rig.store.close()


async def test_a_secret_that_allows_no_window_shows_no_dropdown(tmp_path: Path) -> None:
    rig = await _rig(tmp_path)
    try:
        await rig.store.put_policy(_policy(max_grant_s=0))
        pending = asyncio.create_task(rig.broker.lease(_request()))
        card = await _answer(rig, ANSWER_ONCE)
        await pending
        assert [a.id for a in card.actions] == ["once", "deny"]
    finally:
        await rig.store.close()


async def test_a_revoked_window_stops_serving_immediately(tmp_path: Path) -> None:
    rig = await _rig(tmp_path)
    try:
        await rig.store.put_policy(_policy())
        pending = asyncio.create_task(rig.broker.lease(_request()))
        await _answer(rig, "grant:900")
        await pending
        grant = (await rig.store.list_grants(now=T0, kind=KIND_SECRET))[0]
        assert await rig.store.revoke_grant(grant.id, now=T0) is True

        # The next call has nobody left to ask within the test's patience, so it
        # asks again — which is the point: revoking took effect at once.
        pending = asyncio.create_task(rig.broker.lease(_request()))
        await _answer(rig, ANSWER_DENY)
        assert (await pending).granted is False
        assert len(rig.poster.posts) == 2
    finally:
        await rig.store.close()


# -- refusals ------------------------------------------------------------------


async def test_a_refusal_never_reaches_the_backend_or_a_human(tmp_path: Path) -> None:
    rig = await _rig(tmp_path)
    try:
        result = await rig.broker.lease(_request("vault://nothing-configured"))
        assert result.granted is False
        assert rig.backend.reads == []  # guessing a name costs nothing and learns nothing
        assert rig.poster.posts == []  # and does not wake anybody up
        assert (await rig.store.list_audit())[0].decision == DECISION_NO_POLICY
    finally:
        await rig.store.close()


async def test_an_agent_off_the_list_is_refused_the_same_way(tmp_path: Path) -> None:
    rig = await _rig(tmp_path)
    try:
        await rig.store.put_policy(_policy(subjects="builder"))
        assert (await rig.broker.lease(_request())).granted is False
        assert rig.backend.reads == [] and rig.poster.posts == []
        assert (await rig.store.list_audit())[0].decision == DECISION_NOT_PERMITTED
    finally:
        await rig.store.close()


async def test_a_denial_and_a_silence_are_recorded_apart(tmp_path: Path) -> None:
    rig = await _rig(tmp_path)
    try:
        await rig.store.put_policy(_policy())
        pending = asyncio.create_task(rig.broker.lease(_request()))
        await _answer(rig, ANSWER_DENY)
        assert (await pending).granted is False
        assert rig.backend.reads == []  # denied means not even read

        rig.broker._timeout = 0.05  # nobody is going to click this one
        assert (await rig.broker.lease(_request())).granted is False

        assert [row.decision for row in await rig.store.list_audit()] == [
            DECISION_TIMEOUT, DECISION_DENIED,
        ]
        assert any("in time" in text for _, text in rig.poster.retracted)
    finally:
        await rig.store.close()


async def test_every_authorization_refusal_looks_identical_to_the_caller(
    tmp_path: Path,
) -> None:
    """The property the whole design rests on. Four different reasons, four
    different ledger rows, one thing the caller learns."""
    rig = await _rig(tmp_path)
    try:
        await rig.store.put_policy(_policy(name="mine"))
        await rig.store.put_policy(_policy(name="theirs", subjects="builder"))
        rig.broker._timeout = 0.05

        outcomes = [
            await rig.broker.lease(_request("vault://absent")),  # no policy
            await rig.broker.lease(_request("vault://theirs")),  # not permitted
            await rig.broker.lease(_request("vault://mine")),  # nobody answered
        ]
        pending = asyncio.create_task(rig.broker.lease(_request("vault://mine")))
        await _answer(rig, ANSWER_DENY)  # refused by a human
        outcomes.append(await pending)

        assert [o.granted for o in outcomes] == [False] * 4
        assert {wire_status(o.decision) for o in outcomes} == {WIRE_REFUSED}
        assert [row.decision for row in await rig.store.list_audit()] == [
            DECISION_DENIED, DECISION_TIMEOUT, DECISION_NOT_PERMITTED, DECISION_NO_POLICY,
        ]
    finally:
        await rig.store.close()


async def test_an_engine_with_no_credential_says_unavailable_not_refused(
    tmp_path: Path,
) -> None:
    """Being locked is an operator's problem, not an authorization answer — and
    it leaks nothing about which secrets exist, so it is worth telling apart."""
    rig = await _rig(tmp_path, backend=FakeBackend(authenticated=False))
    try:
        await rig.store.put_policy(_policy())
        result = await rig.broker.lease(_request())
        assert result.granted is False
        assert wire_status(result.decision) == WIRE_UNAVAILABLE
        assert rig.poster.posts == []  # never ask a human for something we can't serve
        assert (await rig.store.list_audit())[0].decision == DECISION_LOCKED
    finally:
        await rig.store.close()


async def test_a_sealed_and_an_unreachable_backend_are_told_apart_in_the_ledger(
    tmp_path: Path,
) -> None:
    sealed = await _rig(tmp_path / "a", backend=FakeBackend(sealed=True))
    gone = await _rig(tmp_path / "b", backend=FakeBackend(reachable=False, sealed=False))
    try:
        for rig, expected in ((sealed, DECISION_SEALED), (gone, DECISION_BACKEND_ERROR)):
            await rig.store.put_policy(_policy())
            assert (await rig.broker.lease(_request())).granted is False
            assert (await rig.store.list_audit())[0].decision == expected
    finally:
        await sealed.store.close()
        await gone.store.close()


async def test_a_backend_that_fails_after_approval_is_recorded_as_such(
    tmp_path: Path,
) -> None:
    rig = await _rig(tmp_path)
    try:
        await rig.store.put_policy(_policy())
        rig.backend.boom = True
        pending = asyncio.create_task(rig.broker.lease(_request()))
        await _answer(rig, ANSWER_ONCE)
        result = await pending

        assert result.granted is False and result.values == {}
        row = (await rig.store.list_audit())[0]
        assert row.decision == DECISION_BACKEND_ERROR
        assert row.approver == APPROVER  # the human did say yes; the store didn't answer
    finally:
        await rig.store.close()


async def test_with_nobody_configured_to_approve_it_refuses_rather_than_hangs(
    tmp_path: Path,
) -> None:
    rig = await _rig(tmp_path, approvers="")
    try:
        await rig.store.put_policy(_policy())
        assert (await rig.broker.lease(_request())).granted is False
        assert (await rig.store.list_audit())[0].decision == DECISION_NO_APPROVER
    finally:
        await rig.store.close()


# -- who may answer ------------------------------------------------------------


async def test_a_click_from_anyone_else_decides_nothing(tmp_path: Path) -> None:
    rig = await _rig(tmp_path)
    try:
        await rig.store.put_policy(_policy())
        pending = asyncio.create_task(rig.broker.lease(_request()))
        card = await _card(rig)

        assert rig.approvals.resolve(_token(card), ANSWER_ONCE, STRANGER) is SecretApprovalOutcome.NOT_ALLOWED
        assert not pending.done()
        # The real approver can still answer the very same card.
        assert rig.approvals.resolve(_token(card), ANSWER_ONCE, APPROVER) is SecretApprovalOutcome.RESOLVED
        result = await pending
        assert result.granted and (await rig.store.list_audit())[0].approver == APPROVER
    finally:
        await rig.store.close()


async def test_an_unknown_token_is_left_for_the_other_click_handlers(
    tmp_path: Path,
) -> None:
    rig = await _rig(tmp_path)
    try:
        assert rig.approvals.resolve("nope", ANSWER_ONCE, APPROVER) is SecretApprovalOutcome.NOT_MINE
    finally:
        await rig.store.close()


async def test_a_card_cannot_be_answered_twice(tmp_path: Path) -> None:
    rig = await _rig(tmp_path)
    try:
        await rig.store.put_policy(_policy())
        pending = asyncio.create_task(rig.broker.lease(_request()))
        card = await _answer(rig, ANSWER_ONCE)
        await pending
        assert rig.approvals.resolve(_token(card), "grant:3600", APPROVER) is SecretApprovalOutcome.NOT_MINE
        assert await rig.store.list_grants(now=T0, kind=KIND_SECRET) == []
        # And the message says what happened, so a stale card can't mislead.
        assert any("Allowed once" in text for _, text in rig.poster.retracted)
    finally:
        await rig.store.close()


async def test_a_garbled_answer_is_a_refusal(tmp_path: Path) -> None:
    """A malformed payload must never be the thing that hands out a credential."""
    rig = await _rig(tmp_path)
    try:
        await rig.store.put_policy(_policy())
        pending = asyncio.create_task(rig.broker.lease(_request()))
        card = await _card(rig)
        rig.approvals.resolve(_token(card), "grant:not-a-number", APPROVER)
        assert (await pending).granted is False
    finally:
        await rig.store.close()


async def test_an_approver_named_by_handle_is_resolved_to_an_id(tmp_path: Path) -> None:
    rig = await _rig(tmp_path, approvers="@roman")
    try:
        await rig.store.put_policy(_policy())
        pending = asyncio.create_task(rig.broker.lease(_request()))
        await _answer(rig, ANSWER_ONCE, user=APPROVER)
        assert (await pending).granted
        assert rig.admin.opened == [APPROVER]  # the card went to their DM
    finally:
        await rig.store.close()


async def test_an_approver_named_by_id_is_taken_as_given(tmp_path: Path) -> None:
    # A platform with no username lookup must still work.
    rig = await _rig(tmp_path, approvers="u1")
    try:
        await rig.store.put_policy(_policy())
        pending = asyncio.create_task(rig.broker.lease(_request()))
        await _answer(rig, ANSWER_ONCE, user=APPROVER)
        assert (await pending).granted
    finally:
        await rig.store.close()


async def test_a_configured_channel_replaces_the_direct_message(tmp_path: Path) -> None:
    rig = await _rig(tmp_path, approval_channel="ch-approvals")
    try:
        await rig.store.put_policy(_policy())
        pending = asyncio.create_task(rig.broker.lease(_request()))
        card = await _answer(rig, ANSWER_ONCE)
        await pending
        assert card.ref.channel_id == "ch-approvals"
        assert rig.admin.opened == []
    finally:
        await rig.store.close()


def _plus(moment: str, seconds: int) -> str:
    from datetime import datetime, timedelta

    return (datetime.fromisoformat(moment) + timedelta(seconds=seconds)).isoformat(
        timespec="seconds"
    )
