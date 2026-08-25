"""The operator surface in chat (ward/chatops.py).

Most of this file is about who may do what, because that is what the surface
changes: administering the broker stops being "holds a private key" and becomes
"is logged into a chat account". Every check that makes that acceptable is
pinned here — the approver list, the direct-message rule, and the fact that both
are re-asked on the click and on the submission rather than trusted from the one
before.

The rest pins what the surface will not do: no route that hands out a
credential, and no value echoed back after it is stored.
"""

from pathlib import Path
from typing import Any

import pytest

from crucible.interactions.dispatcher import InteractionDispatcher
from crucible.interactions.pending_ui import PendingUiRequests
from crucible.interactions.screens import (
    ScreenRegistry,
    ScreenState,
    state_from_context,
)
from crucible.ports.chat.interactions import form_from_json
from crucible.ports.chat.types import ConversationRef, Form
from tests.fakes.fake_chat import FakeChat as PostingChat
from tests.fakes.presence import presence_of
from ward.approvers import Approvers
from ward.chatops import (
    HANDLER,
    OperatorForms,
    PendingOperatorForms,
    WardScreen,
)
from ward.decisions import KIND_OPERATOR
from ward.operations import Operations
from ward.ports import BackendStatus, UnlockMaterial
from ward.store import SecretPolicyRecord, WardStore

OPERATOR = "u-operator"
STRANGER = "u-stranger"
DM = "dm-with-the-operator"
CHANNEL = "a-shared-channel"
T0 = "2026-08-25T09:00:00+00:00"


class FakeBackend:
    def __init__(self, **state: Any) -> None:
        self.values: dict[str, dict[str, str]] = {}
        self.state = BackendStatus(
            reachable=state.get("reachable", True),
            sealed=state.get("sealed", False),
            authenticated=state.get("authenticated", True),
        )
        self.rotations = 0

    async def status(self) -> BackendStatus:
        return self.state

    async def unlock(self, material: UnlockMaterial) -> BackendStatus:
        self.state = BackendStatus(reachable=True, sealed=False, authenticated=True)
        return self.state

    async def read(self, ref) -> str:
        return "value"

    async def write(self, name: str, values) -> None:
        self.values[name] = dict(values)

    async def delete(self, name: str) -> None:
        self.values.pop(name, None)

    async def names(self) -> list[str]:
        return sorted(self.values)

    async def rotate(self) -> str:
        self.rotations += 1
        return "fresh"

    async def close(self) -> None:
        return None


class FakeBroker:
    """Only the two verbs the surface uses."""

    def __init__(self, backend: FakeBackend) -> None:
        self.backend = backend
        self.unlocked: list[UnlockMaterial] = []

    async def status(self) -> BackendStatus:
        return await self.backend.status()

    async def unlock(self, material: UnlockMaterial) -> BackendStatus:
        self.unlocked.append(material)
        return await self.backend.unlock(material)

    async def lease(self, request):  # pragma: no cover - not reachable from chat
        raise AssertionError("the chat surface must not lease")


class FakeChat:
    """The bot's account: opens direct messages, posts outcomes."""

    def __init__(self, *, direct: str = DM) -> None:
        self._direct = direct
        self.posted: list[tuple[str, str]] = []

    async def open_direct(self, user_id: str) -> str:
        return self._direct

    async def resolve_username(self, username: str) -> str | None:
        return None

    async def post_reply(self, ref: ConversationRef, text: str, **_: Any) -> None:
        self.posted.append((ref.channel_id, text))


async def _handle(forms, store, token: str, values: dict, user_id: str) -> None:
    """What the dispatcher does for a routed form: look the record up, retire it,
    hand it to the named handler. Ownership is settled before this point."""
    record = await store.get_form(token)
    assert record is not None and record.handler == HANDLER
    await store.delete_form(token)
    await forms.handle(record, values, user_id)


async def _open(screen, *, user_id: str, ref: ConversationRef | None = None):
    """What the engine does to open a card: ask admission, then draw.

    A refusal is not a view — the engine posts nothing and answers privately —
    so these tests assert on the refusal string, and on the fact that no view
    was produced at all.
    """
    denial = await screen.admits(user_id=user_id, ref=ref if ref is not None else _here())
    if denial:
        return denial
    return await screen.render(ScreenState(screen="ward"), user_id=user_id)


async def _rig(tmp_path: Path, **state: Any):
    store = WardStore(tmp_path / "ward.db")
    backend = FakeBackend(**state)
    broker = FakeBroker(backend)
    chat = FakeChat()
    approvers = Approvers(OPERATOR, chat)  # type: ignore[arg-type]
    operations = Operations(backend, store, store)  # type: ignore[arg-type]
    pending = PendingOperatorForms()
    screen = WardScreen(
        broker, operations, approvers, chat, store, store, pending  # type: ignore[arg-type]
    )
    forms = OperatorForms(
        broker, operations, approvers, chat, chat, store, pending  # type: ignore[arg-type]
    )
    return screen, forms, store, broker, chat, backend


def _here(channel: str = DM) -> ConversationRef:
    return ConversationRef(channel_id=channel, conversation_id=channel, message_id=channel)


def _text(view) -> str:
    return "\n".join(card.text for card in view.cards)


def _actions(view) -> list:
    return [action for card in view.cards for action in card.actions]


# -- who may see it ----------------------------------------------------------------


async def test_a_stranger_is_refused_before_anything_is_drawn(tmp_path: Path) -> None:
    """The refusal is not a view. Nothing is rendered, so there is nothing that
    could be posted — not the status, not the names, not even whether a store
    exists."""
    screen, _, store, *_ = await _rig(tmp_path)
    try:
        denial = await screen.admits(user_id=STRANGER, ref=_here())
        assert "not on its list" in denial
        assert "secrets:" not in denial
    finally:
        await store.close()


async def test_in_a_channel_nothing_is_posted_at_all(tmp_path: Path) -> None:
    """Driven through the dispatcher, because the guarantee is about posting
    rather than about a return value: the card lists secret names, and in a
    channel it would show them to the room."""
    screen, _, store, *_ = await _rig(tmp_path)
    chat = PostingChat()
    screens = ScreenRegistry()
    screens.register(screen)
    dispatcher = InteractionDispatcher(
        store, presence_of(chat, agent="ward"), PendingUiRequests(), store, screens=screens
    )
    try:
        opened = await dispatcher.open_screen(
            "ward", "ward", channel_id=CHANNEL, conversation_id=CHANNEL,
            kind="channel", user_id=OPERATOR,
        )
        assert opened.owned is True
        assert "direct message" in opened.refused
        assert chat.posted_cards == []  # the room saw nothing
    finally:
        await store.close()


async def test_in_a_direct_message_the_card_is_posted(tmp_path: Path) -> None:
    """The other half of the same guarantee, so the refusal above is not just a
    screen that never works."""
    screen, _, store, *_ = await _rig(tmp_path)
    chat = PostingChat()
    screens = ScreenRegistry()
    screens.register(screen)
    dispatcher = InteractionDispatcher(
        store, presence_of(chat, agent="ward"), PendingUiRequests(), store, screens=screens
    )
    try:
        opened = await dispatcher.open_screen(
            "ward", "ward", channel_id=DM, conversation_id=DM,
            kind="dm", user_id=OPERATOR,
        )
        assert opened.owned is True and opened.refused == ""
        assert len(chat.posted_cards) == 1
    finally:
        await store.close()


async def test_a_click_from_a_stranger_is_refused(tmp_path: Path) -> None:
    """A redraw carries whoever pressed the button, not whoever opened the card,
    so admission is asked again — without a ref, since the message exists."""
    screen, _, store, *_ = await _rig(tmp_path)
    try:
        assert await screen.admits(user_id=STRANGER, ref=None) != ""
        assert await screen.admits(user_id=OPERATOR, ref=None) == ""
    finally:
        await store.close()


# -- what it shows -----------------------------------------------------------------


async def test_the_menu_names_which_half_is_missing(tmp_path: Path) -> None:
    """Sealed and locked need different material, so the card says which — and
    offers Unseal only when the store itself is closed."""
    screen, _, store, *_ = await _rig(tmp_path, sealed=True, authenticated=False)
    try:
        view = await _open(screen, user_id=OPERATOR)
        assert "sealed" in _text(view)
        assert {"unlock", "unseal"} <= {a.id for a in _actions(view)}
    finally:
        await store.close()


async def test_a_locked_store_is_not_offered_the_unseal_key(tmp_path: Path) -> None:
    """Vault is up and unsealed; only the credential is missing. Offering to
    take the unseal key here would put the key to everything through chat for
    nothing."""
    screen, _, store, *_ = await _rig(tmp_path, sealed=False, authenticated=False)
    try:
        view = await _open(screen, user_id=OPERATOR)
        ids = {a.id for a in _actions(view)}
        assert "unlock" in ids and "unseal" not in ids
    finally:
        await store.close()


async def test_an_open_store_offers_neither(tmp_path: Path) -> None:
    screen, _, store, *_ = await _rig(tmp_path)
    try:
        view = await _open(screen, user_id=OPERATOR)
        assert "open" in _text(view)
        assert not {"unlock", "unseal"} & {a.id for a in _actions(view)}
    finally:
        await store.close()


async def test_the_surface_never_offers_a_credential_back(tmp_path: Path) -> None:
    """`rotate` and `cert` exist in the CLI and must not exist here: their whole
    output is a credential, and a credential in a message is a credential in the
    platform's database."""
    screen, _, store, *_ = await _rig(tmp_path, sealed=True, authenticated=False)
    try:
        seen = set()
        for view in (_MENU_VIEW := ("", "secrets", "windows", "ledger")):
            rendered = await screen.render(
                ScreenState(screen="ward", data={"view": view}), user_id=OPERATOR
            )
            seen |= {a.id for a in _actions(rendered)}
        assert not {"rotate", "cert"} & seen
    finally:
        await store.close()


async def test_a_window_can_be_closed_from_the_card(tmp_path: Path) -> None:
    screen, _, store, *_ = await _rig(tmp_path)
    try:
        from crucible.store.base import ApprovalGrant
        await store.create_grant(
            ApprovalGrant(
                id="gr_1", kind="secret", principal="assistant", scope="github-token",
                granted_by=OPERATOR, granted_at=T0, expires_at="2099-01-01T00:00:00+00:00",
            )
        )
        listed = await screen.render(
            ScreenState(screen="ward", data={"view": "windows"}), user_id=OPERATOR
        )
        revoke = next(a for a in _actions(listed) if a.id.startswith("rv-"))
        carried = state_from_context(revoke.context)
        assert carried is not None
        after = await screen.render(carried, user_id=OPERATOR)

        assert "No windows are open" in _text(after)
        rows = await store.list_audit(limit=5, kind=KIND_OPERATOR)
        assert [(r.principal, r.decision) for r in rows] == [(OPERATOR, "revoke")]
    finally:
        await store.close()


# -- the modals --------------------------------------------------------------------


async def test_the_unlock_modal_asks_only_for_the_credential(tmp_path: Path) -> None:
    screen, _, store, *_ = await _rig(tmp_path, sealed=False, authenticated=False)
    try:
        view = await _open(screen, user_id=OPERATOR)
        button = next(a for a in _actions(view) if a.id == "unlock")
        record = await store.get_form(str(button.context["form"]))
        assert record is not None
        form = form_from_json(record.spec)
        assert [f.name for f in form.fields] == ["secret_id"]
        # Masked where the platform can: this is typed on a phone, in public.
        assert all(f.type == "password" for f in form.fields)
    finally:
        await store.close()


async def test_a_submitted_credential_opens_the_store_and_never_comes_back(
    tmp_path: Path,
) -> None:
    screen, forms, store, broker, chat, _ = await _rig(
        tmp_path, sealed=False, authenticated=False
    )
    try:
        view = await _open(screen, user_id=OPERATOR)
        token = str(next(a for a in _actions(view) if a.id == "unlock").context["form"])

        await _handle(forms, store, token, {"secret_id": "sid-1"}, OPERATOR)

        assert broker.unlocked == [UnlockMaterial(unseal_key="", auth_secret="sid-1")]
        channel, text = chat.posted[-1]
        assert channel == DM  # to the person, not to wherever the modal was opened
        assert "open" in text
        assert "sid-1" not in text  # the value is not echoed, ever
        assert "rotate" in text  # and it passed through chat, so: replace it
    finally:
        await store.close()


async def test_a_submission_from_a_stranger_is_refused(tmp_path: Path) -> None:
    """The submission arrives from the platform with its own user id. Trusting
    the click that opened the dialog would let a stranger finish somebody else's
    modal."""
    screen, forms, store, broker, chat, _ = await _rig(
        tmp_path, sealed=False, authenticated=False
    )
    try:
        view = await _open(screen, user_id=OPERATOR)
        token = str(next(a for a in _actions(view) if a.id == "unlock").context["form"])

        await _handle(forms, store, token, {"secret_id": "sid-1"}, STRANGER)
        assert broker.unlocked == []
        assert "not on its list" in chat.posted[-1][1]
    finally:
        await store.close()


async def test_a_stored_value_is_written_and_not_repeated(tmp_path: Path) -> None:
    screen, forms, store, _, chat, backend = await _rig(tmp_path)
    try:
        view = await _open(screen, user_id=OPERATOR)
        token = str(next(a for a in _actions(view) if a.id == "store").context["form"])

        await _handle(
            forms, store, token, {"name": "github-token", "value": "ghp_x"}, OPERATOR
        )

        assert backend.values["github-token"] == {"value": "ghp_x"}
        text = chat.posted[-1][1]
        assert "github-token" in text and "ghp_x" not in text
        # No policy yet, so it says so — a stored secret nobody may reach is the
        # commonest way this goes quietly wrong.
        assert "no policy" in text
        rows = await store.list_audit(limit=5, kind=KIND_OPERATOR)
        assert [(r.principal, r.decision, r.scope) for r in rows] == [
            (OPERATOR, "set", "github-token")
        ]
    finally:
        await store.close()


async def test_a_secret_with_a_policy_is_not_nagged_about(tmp_path: Path) -> None:
    screen, forms, store, _broker, chat, _backend = await _rig(tmp_path)
    try:
        await store.put_policy(
            SecretPolicyRecord(
                name="github-token", approval="always", max_grant_s=900,
                subjects="assistant", description="", created_at=T0, updated_at=T0,
            )
        )
        view = await _open(screen, user_id=OPERATOR)
        token = str(next(a for a in _actions(view) if a.id == "store").context["form"])
        await _handle(
            forms, store, token, {"name": "github-token", "value": "ghp_x"}, OPERATOR
        )
        assert "no policy" not in chat.posted[-1][1]
    finally:
        await store.close()


async def test_a_form_this_process_did_not_open_is_refused(tmp_path: Path) -> None:
    """The verb lives in memory, so a broker that restarted between opening a
    modal and answering it no longer knows what the form was for. Guessing would
    mean acting on a credential for an unknown reason."""
    _, forms, store, broker, chat, _ = await _rig(tmp_path)
    try:
        from crucible.store.base import FormRecord

        stale = FormRecord(
            token="stale", agent="ward", channel_id="", conversation_id="", kind="",
            spec="{}", created_at=T0, post_id="", handler=HANDLER,
        )
        await forms.handle(stale, {"secret_id": "sid-1"}, OPERATOR)
        assert broker.unlocked == []
        assert "no longer open" in chat.posted[-1][1]
    finally:
        await store.close()


async def test_a_modal_is_one_shot(tmp_path: Path) -> None:
    screen, forms, store, broker, *_ = await _rig(
        tmp_path, sealed=False, authenticated=False
    )
    try:
        view = await _open(screen, user_id=OPERATOR)
        token = str(next(a for a in _actions(view) if a.id == "unlock").context["form"])
        await _handle(forms, store, token, {"secret_id": "sid-1"}, OPERATOR)
        # The record is gone, so the dispatcher would never route a second one;
        # and the verb is gone too, so even a replayed record is refused.
        assert await store.get_form(token) is None
        assert len(broker.unlocked) == 1
    finally:
        await store.close()


@pytest.mark.parametrize("values", [{}, {"secret_id": "   "}])
async def test_an_empty_submission_does_not_reach_the_store(
    tmp_path: Path, values: dict
) -> None:
    screen, forms, store, broker, chat, _ = await _rig(
        tmp_path, sealed=False, authenticated=False
    )
    try:
        view = await _open(screen, user_id=OPERATOR)
        token = str(next(a for a in _actions(view) if a.id == "unlock").context["form"])
        await _handle(forms, store, token, values, OPERATOR)
        assert broker.unlocked == []
        assert "Nothing was filled in" in chat.posted[-1][1]
    finally:
        await store.close()


async def test_what_an_operator_did_is_readable_afterwards(tmp_path: Path) -> None:
    """An audit row nobody's reader shows is not an audit row. Both kinds live
    in one ledger and both are what happened here."""
    screen, forms, store, *_ = await _rig(tmp_path, sealed=False, authenticated=False)
    try:
        view = await _open(screen, user_id=OPERATOR)
        token = str(next(a for a in _actions(view) if a.id == "unlock").context["form"])
        await _handle(forms, store, token, {"secret_id": "sid-1"}, OPERATOR)

        ledger = await screen.render(
            ScreenState(screen="ward", data={"view": "ledger"}), user_id=OPERATOR
        )
        text = _text(ledger)
        assert "unlock" in text and OPERATOR in text and "(operator)" in text
    finally:
        await store.close()


# -- editing a policy from chat -------------------------------------------------------


async def _policy_form(screen, store, name: str = "github-token") -> str:
    """Open the policy modal on a secret and return its form token."""
    button = await _policy_button(screen, name)
    return str(button.context["form"])


async def _policy_button(screen, name: str = "github-token"):
    view = await screen.render(
        ScreenState(screen="ward", data={"view": "secrets"}), user_id=OPERATOR
    )
    return next(a for a in _actions(view) if a.id == f"ed-{name}")


async def _spec(store, token: str) -> Form:
    record = await store.get_form(token)
    assert record is not None
    return form_from_json(record.spec)


async def _stored(tmp_path: Path, store, **over) -> None:
    base = dict(
        name="github-token", approval="always", max_grant_s=900,
        subjects="assistant", description="", created_at=T0, updated_at=T0,
    )
    base.update(over)
    await store.put_policy(SecretPolicyRecord(**base))  # type: ignore[arg-type]


async def test_a_policy_is_edited_from_the_card(tmp_path: Path) -> None:
    screen, forms, store, _broker, chat, backend = await _rig(tmp_path)
    try:
        backend.values["github-token"] = {"value": "x"}
        await _stored(tmp_path, store)
        token = await _policy_form(screen, store)

        await _handle(forms, store, token, {
            "subjects": "assistant, builder",
            "auto_commands": "gh release create *",
        }, OPERATOR)

        saved = await store.get_policy("github-token")
        assert saved is not None
        assert saved.subjects == "assistant, builder"
        assert saved.rules == (("gh", "release", "create", "*"),)
        # Approval and window were not touched, so they are as they were.
        assert saved.approval == "always" and saved.max_grant_s == 900
    finally:
        await store.close()


async def test_an_unreadable_rule_leaves_the_policy_alone(tmp_path: Path) -> None:
    """A rule that cannot be read would be a rule that silently never fires, so
    it is refused before the store sees it — and refusing must not half-apply
    the rest of the form."""
    screen, forms, store, _broker, chat, backend = await _rig(tmp_path)
    try:
        backend.values["github-token"] = {"value": "x"}
        await _stored(tmp_path, store)
        token = await _policy_form(screen, store)

        await _handle(forms, store, token, {
            "subjects": "everyone", "auto_commands": "*",
        }, OPERATOR)

        saved = await store.get_policy("github-token")
        assert saved is not None and saved.subjects == "assistant"  # untouched
        assert "never" in chat.posted[-1][1]  # and told why
    finally:
        await store.close()


async def test_the_change_is_recorded_with_what_it_changed(tmp_path: Path) -> None:
    """Handing an agent access from a chat session has to be answerable
    afterwards, and the diff is the whole of that answer."""
    screen, forms, store, _broker, _chat, backend = await _rig(tmp_path)
    try:
        backend.values["github-token"] = {"value": "x"}
        await _stored(tmp_path, store)
        token = await _policy_form(screen, store)

        await _handle(forms, store, token, {"subjects": "builder"}, OPERATOR)

        row = (await store.list_audit(limit=1, kind=KIND_OPERATOR))[0]
        assert row.principal == OPERATOR and row.decision == "policy"
        assert "assistant" in row.scope and "builder" in row.scope
    finally:
        await store.close()


# -- the first policy, from the same modal ---------------------------------------------


async def test_a_secret_stored_from_chat_can_be_made_to_work_from_chat(
    tmp_path: Path,
) -> None:
    """The chain the surface exists for: Store a secret, then give it a policy,
    without the machine that holds `operator.key`. Storing one that could only
    ever be made to work from that machine would have hollowed out the point."""
    screen, forms, store, _broker, chat, backend = await _rig(tmp_path)
    try:
        view = await _open(screen, user_id=OPERATOR)
        store_button = next(a for a in _actions(view) if a.id == "store")
        await _handle(
            forms, store, str(store_button.context["form"]),
            {"name": "test", "value": "s3cret"}, OPERATOR,
        )
        assert backend.values["test"] == {"value": "s3cret"}

        token = await _policy_form(screen, store, "test")
        await _handle(forms, store, token, {"subjects": "assistant"}, OPERATOR)

        saved = await store.get_policy("test")
        assert saved is not None and saved.subjects == "assistant"
        # And the defaults are the strict ones the modal showed, not whatever
        # happened to be convenient: ask a human every time, no window, no rule.
        assert saved.approval == "always"
        assert saved.max_grant_s == 0 and saved.rules == ()
        assert "created" in chat.posted[-1][1]
    finally:
        await store.close()


async def test_the_first_policy_must_name_somebody(tmp_path: Path) -> None:
    """One naming nobody leaves the secret exactly as unreachable, so accepting
    it would be a second dead end wearing a tick."""
    screen, forms, store, _broker, chat, backend = await _rig(tmp_path)
    try:
        backend.values["test"] = {"value": "x"}
        token = await _policy_form(screen, store, "test")

        await _handle(forms, store, token, {"approval": "never"}, OPERATOR)

        assert await store.get_policy("test") is None
        assert "🔴" in chat.posted[-1][1]
    finally:
        await store.close()


async def test_the_modal_says_which_of_the_two_it_is(tmp_path: Path) -> None:
    """A form whose intro promises "left as they are" when there is nothing to
    leave is a form that lies about what an empty field does. And the agents
    field is required exactly when a default for it would be useless, so the
    refusal above is a backstop rather than the first the operator hears of it."""
    screen, _forms, store, _broker, _chat, backend = await _rig(tmp_path)
    try:
        backend.values["fresh"] = {"value": "x"}
        await _stored(tmp_path, store)
        backend.values["github-token"] = {"value": "x"}

        first = await _spec(store, await _policy_form(screen, store, "fresh"))
        assert "No policy yet" in first.intro
        assert first.submit_label == "Create"
        assert next(f for f in first.fields if f.name == "subjects").optional is False

        again = await _spec(store, await _policy_form(screen, store, "github-token"))
        assert "left as they are" in again.intro
        assert again.submit_label == "Save"
        assert next(f for f in again.fields if f.name == "subjects").optional is True
    finally:
        await store.close()


async def test_the_button_and_the_note_say_what_is_missing(tmp_path: Path) -> None:
    """`impi ward ls` distinguishes "no policy" from "no subjects" because the
    two want different things done about them; the card used to say neither."""
    screen, _forms, store, _broker, _chat, backend = await _rig(tmp_path)
    try:
        backend.values["fresh"] = {"value": "x"}
        backend.values["nameless"] = {"value": "x"}
        await _stored(tmp_path, store, name="nameless", subjects="")
        view = await screen.render(
            ScreenState(screen="ward", data={"view": "secrets"}), user_id=OPERATOR
        )
        assert "no policy" in _text(view)
        assert "no agents named" in _text(view)
        assert (await _policy_button(screen, "fresh")).label == "Set policy"
        assert (await _policy_button(screen, "nameless")).label == "Edit"
    finally:
        await store.close()


async def test_creating_one_is_recorded_as_a_creation(tmp_path: Path) -> None:
    """"Who gave assistant access to this" and "who widened what it already had"
    are different questions, and the ledger is where both are answered."""
    screen, forms, store, _broker, _chat, backend = await _rig(tmp_path)
    try:
        backend.values["test"] = {"value": "x"}
        token = await _policy_form(screen, store, "test")

        await _handle(forms, store, token, {"subjects": "assistant"}, OPERATOR)

        row = (await store.list_audit(limit=1, kind=KIND_OPERATOR))[0]
        assert row.principal == OPERATOR and row.decision == "policy"
        assert "created" in row.scope and "assistant" in row.scope
    finally:
        await store.close()


async def test_a_stranger_cannot_edit_a_policy(tmp_path: Path) -> None:
    screen, forms, store, _broker, _chat, backend = await _rig(tmp_path)
    try:
        backend.values["github-token"] = {"value": "x"}
        await _stored(tmp_path, store)
        token = await _policy_form(screen, store)

        await _handle(forms, store, token, {"subjects": "attacker"}, STRANGER)

        saved = await store.get_policy("github-token")
        assert saved is not None and saved.subjects == "assistant"
    finally:
        await store.close()
