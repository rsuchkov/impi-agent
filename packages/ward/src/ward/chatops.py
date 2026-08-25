"""The operator's surface in chat: `/ward`.

Everything an operator does today needs the machine that holds `operator.key`.
Two of those things do not wait for that machine. After an unplanned restart the
store is sealed and every agent is refused until somebody unlocks it, and the
answer to "why was my agent refused" is a command away from whoever is holding a
phone. So a subset of the operator CLI lives here, on the receiver the approval
cards already come back through.

Three rules shape it, and each is a decision rather than an implementation
detail:

**No arguments, ever.** `/ward` takes none: it opens a card, and everything else
is a click or a modal. A slash command with arguments is a message that becomes
a public post the moment the command word is mistyped — which is how a key ends
up in a channel. There is nothing here to mistype.

**Direct messages only.** The card is posted where the command was invoked, so
in a channel it would show secret names and policies to everyone in the room.
Invoked anywhere but a one-to-one with this bot, it refuses and says where to go.

**Nothing that hands out a credential.** `rotate` and `cert` exist in the CLI and
are deliberately absent here: their whole output is a credential, and a
credential in a chat message is a credential in the platform's database. What
this surface accepts is input; what it returns is state.
"""

import logging
import secrets as tokens
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any

from crucible.approvals import GRANT_LADDER, humanize
from crucible.approvals.card import one_line
from crucible.interactions.screens import ScreenState, View, screen_action
from crucible.ports.chat.admin import ChatAdmin
from crucible.ports.chat.client import ChatClient
from crucible.ports.chat.interactions import form_to_json
from crucible.ports.chat.types import (
    Action,
    Card,
    ConversationRef,
    Form,
    FormField,
)
from crucible.store.base import ApprovalAudit, ApprovalStore, FormRecord, FormStore
from ward.approvers import Approvers
from ward.autorules import RuleError, parse
from ward.decisions import KIND_OPERATOR
from ward.operations import Operations
from ward.ports import SecretBackendError, SecretLeasing, UnlockMaterial
from wardline.wire import APPROVAL_ALWAYS, APPROVALS

logger = logging.getLogger(__name__)

# Mattermost refuses a dialog whose title is too long, and a secret's name is a
# caller-supplied-ish string that lands in one.
_TITLE_MAX = 24

COMMAND = "ward"

# The name ward's forms are written with, so the receiver routes them here
# rather than into a conversation. See FormRecord.handler.
HANDLER = "ward-operator"

# What a click asks for. Views redraw the card; actions open a modal.
_VIEW = "view"
_MENU, _SECRETS, _WINDOWS, _LEDGER = "", "secrets", "windows", "ledger"
_REVOKE = "revoke"

# The verb prefix an Edit button carries, so the handler knows which secret's
# policy came back — a modal has no other way to say what it was opened for.
_POLICY = "policy"

# Field names inside the modals, and the two that carry material.
_F_UNSEAL, _F_SECRET_ID = "unseal_key", "secret_id"
_F_NAME, _F_VALUE = "name", "value"
_F_SUBJECTS, _F_APPROVAL, _F_GRANT, _F_RULES = (
    "subjects", "approval", "max_grant", "auto_commands"
)

# What a secret with no policy counts as, so one modal both creates and edits.
# Every field is the strictest reading — nobody may ask, every use asks a human,
# no window, no rule — because widening is the direction an operator should have
# to type in. Read by the modal for its placeholders and by the handler for its
# defaults, so a placeholder cannot promise something the submission does not do.
# Shared, so nothing in it is mutable.
_NEW_POLICY: dict[str, Any] = {
    "subjects": "",
    "approval": APPROVAL_ALWAYS,
    "max_grant_s": 0,
    "description": "",
    "auto_commands": (),
}

# How much of a list a card shows. A phone is the point of this surface, and a
# hundred rows on a phone is a wall nobody reads.
_LIMIT = 10

_NOT_YOURS = (
    "🔐 This is the secret broker's operator surface, and you are not on its list."
)
_DM_ONLY = (
    "🔐 Ask me this in a direct message. Here I would post secret names and "
    "policies to everyone in the channel."
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class WardScreen:
    """The card `/ward` opens, and every redraw of it.

    Who may see it is ``admits``, which the engine asks before this draws
    anything and again on every click. What is here is only what to draw.
    """

    command = COMMAND

    def __init__(
        self,
        broker: SecretLeasing,
        operations: Operations,
        approvers: Approvers,
        admin: ChatAdmin,
        forms: FormStore,
        ledger: ApprovalStore,
        pending: "PendingOperatorForms",
    ) -> None:
        self._broker = broker
        self._ops = operations
        self._approvers = approvers
        self._admin = admin
        self._forms = forms
        self._ledger = ledger
        self._pending = pending

    async def admits(self, *, user_id: str, ref: ConversationRef | None) -> str:
        """Whether this card may appear at all — asked before anything is drawn.

        Two questions, and neither belongs in ``render``: a refusal must not be
        a view, because a view gets posted, and posting it would put the answer
        into the very conversation the card is refusing to appear in.

        ``ref`` is present only when a card is about to be created; on a click
        the message exists already, so what is left is whether this person may
        drive it. A click carries whoever pressed it, not whoever opened it.
        """
        if not await self._approvers.allows(user_id):
            return _NOT_YOURS
        if ref is not None and not await self._is_direct(user_id, ref):
            return _DM_ONLY
        return ""

    async def render(self, state: ScreenState, *, user_id: str) -> View:
        view = str(state.data.get(_VIEW) or _MENU)
        if view == _SECRETS:
            return await self._secrets(state, user_id)
        if view == _WINDOWS:
            return await self._windows(state, user_id)
        if view == _LEDGER:
            return await self._recent(state)
        return await self._menu(state, user_id)

    async def _is_direct(self, user_id: str, ref: ConversationRef) -> bool:
        """Whether this conversation is the one-to-one with the person asking.

        Asked of the platform rather than inferred: opening the direct channel
        returns the id this bot and that person share, and comparing it is both
        the "is it a DM" check and the "is it THEIR DM" check.
        """
        try:
            direct = await self._admin.open_direct(user_id)
        except Exception:
            logger.warning("ward screen: cannot resolve the direct channel", exc_info=True)
            return False
        return bool(direct) and direct == ref.channel_id

    # -- the views ------------------------------------------------------------

    async def _menu(self, state: ScreenState, user_id: str) -> View:
        state = state.with_data(**{_VIEW: _MENU})
        status = await self._broker.status()
        if status.usable:
            headline = "🔓 **secrets: open**"
        elif not status.reachable:
            headline = "🔴 **secrets: the store is unreachable**"
        elif status.sealed:
            headline = "🔒 **secrets: sealed** — the store itself is closed"
        else:
            headline = "🔒 **secrets: locked** — the broker holds no credential"
        detail = f"\n{one_line(status.detail)}" if status.detail else ""

        actions: list[Action] = []
        if not status.usable:
            # Two doors, named for what each needs, so the choice to put the key
            # to the whole store through a chat platform is made on purpose and
            # not by filling in whichever field the form happened to show.
            actions.append(await self._form_action(
                state, user_id, id="unlock", label="Unlock", form=_unlock_form()
            ))
            if status.sealed:
                actions.append(await self._form_action(
                    state, user_id, id="unseal", label="Unseal…", form=_unseal_form()
                ))
        actions.append(await self._form_action(
            state, user_id, id="store", label="Store a secret", form=_store_form()
        ))
        actions.extend((
            screen_action(state.with_data(**{_VIEW: _SECRETS}), id="ls", label="Secrets"),
            screen_action(state.with_data(**{_VIEW: _WINDOWS}), id="grants", label="Windows"),
            screen_action(state.with_data(**{_VIEW: _LEDGER}), id="audit", label="Ledger"),
        ))
        return View.of(f"{headline}{detail}", tuple(actions))

    async def _secrets(self, state: ScreenState, user_id: str) -> View:
        answer = await self._ops.list_secrets()
        if answer.get("error"):
            return self._back(state, f"🔴 {one_line(str(answer['error']))}")
        entries = answer.get("secrets") or []
        if not entries:
            return self._back(state, "No secrets stored yet.")
        cards: list[Card] = []
        for entry in entries[:_LIMIT]:
            name = str(entry["name"])
            policy = entry.get("policy") or {}
            # Two ways to be unreachable, and they need different things done
            # about them — one wants a policy, the other wants a name in the one
            # that exists. "Unreachable" alone left the reader to guess which.
            if not policy:
                note = "_no policy — unreachable by every agent_"
            elif not policy.get("subjects"):
                note = "_no agents named — unreachable by every agent_"
            else:
                rules = policy.get("auto_commands") or []
                automatic = f", {len(rules)} auto-rule(s)" if rules else ""
                note = (
                    f"{policy['approval']}{automatic}, "
                    f"for: {one_line(str(policy['subjects']))}"
                )
            edit = await self._form_action(
                state, user_id, id=f"ed-{name}",
                label="Edit" if policy else "Set policy",
                form=_policy_form(name, policy), verb=f"{_POLICY}:{name}",
            )
            cards.append(Card(text=f"`{one_line(name)}` — {note}", actions=(edit,)))
        if len(entries) > _LIMIT:
            cards.append(Card(text=_capped_note(len(entries) - _LIMIT)))
        cards.append(Card(text="", actions=(self._home(state),)))
        return View(cards=tuple(cards))

    async def _windows(self, state: ScreenState, user_id: str) -> View:
        # A click may carry a window to close; do that first, then redraw the
        # list it came from, so the card always shows the state after the act.
        closing = str(state.data.pop(_REVOKE, "") or "")
        if closing:
            await self._ops.revoke_grant(closing)
            await self._record(user_id, "revoke", closing)
        grants = (await self._ops.list_grants()).get("grants") or []
        if not grants:
            return self._back(state, "No windows are open.")
        cards: list[Card] = []
        for grant in grants[:_LIMIT]:
            text = (
                f"`{one_line(str(grant['agent']))}` → `{one_line(str(grant['secret']))}`\n"
                f"until {one_line(str(grant['expires_at']))}"
            )
            revoke = screen_action(
                state.with_data(**{_VIEW: _WINDOWS, _REVOKE: str(grant["id"])}),
                id=f"rv-{grant['id']}", label="Revoke", style="danger",
            )
            cards.append(Card(text=text, actions=(revoke,)))
        cards.append(Card(text="", actions=(self._home(state),)))
        return View(cards=tuple(cards))

    async def _recent(self, state: ScreenState) -> View:
        rows = (await self._ops.list_audit(limit=_LIMIT)).get("audit") or []
        if not rows:
            return self._back(state, "Nothing has happened yet.")
        lines = [
            f"• `{one_line(str(r['decision']))}` {one_line(str(r['agent']))} → "
            f"`{one_line(str(r['secret']))}`  _{one_line(str(r['at']))}_"
            + ("" if r.get("kind", "secret") == "secret" else "  _(operator)_")
            for r in rows
        ]
        return self._back(state, "\n".join(lines))

    def _back(self, state: ScreenState, text: str) -> View:
        return View.of(text, (self._home(state),))

    async def _record(self, user_id: str, verb: str, scope: str) -> None:
        await _record_operator(self._ledger, user_id, verb, scope)

    def _home(self, state: ScreenState) -> Action:
        return screen_action(state.with_data(**{_VIEW: _MENU}), id="home", label="← Back")

    # -- modals ---------------------------------------------------------------

    async def _form_action(
        self, state: ScreenState, user_id: str, *, id: str, label: str, form: Form,
        verb: str = "",
    ) -> Action:
        """A button that opens a modal rather than redrawing.

        The receiver opens the dialog from a stored spec, so the form is written
        down here and the button carries only its token — a spec travelling
        through the platform would be a spec the platform could rewrite.
        """
        token = tokens.token_hex(16)
        await self._forms.create_form(
            FormRecord(
                token=token,
                agent=state.agent or COMMAND,
                channel_id="",  # answered by handler, never posted back to a channel
                conversation_id="",
                kind="",
                spec=form_to_json(form),
                created_at=_now(),
                post_id="",
                # Routed by name: this form's values are the application's, and
                # a form that fell through to the agent path would put a
                # credential into a conversation.
                handler=HANDLER,
            )
        )
        self._pending.register(token, verb or id)
        return Action(id=id, label=label, context={"form": token})


def _unlock_form() -> Form:
    return Form(
        title="Unlock the store",
        intro="The broker's credential — the secret id from `ward-recovery.txt`.",
        submit_label="Unlock",
        fields=(
            FormField(
                name=_F_SECRET_ID, label="Broker credential (secret id)", type="password"
            ),
        ),
    )


def _unseal_form() -> Form:
    return Form(
        title="Unseal the store",
        intro=(
            "The store itself is sealed, so this needs the unseal key as well. "
            "It passes through the chat platform, and unlike the credential it "
            "cannot be rotated afterwards — see docs/secrets.md."
        ),
        submit_label="Unseal",
        fields=(
            FormField(name=_F_UNSEAL, label="Store unseal key", type="password"),
            FormField(
                name=_F_SECRET_ID, label="Broker credential (secret id)", type="password"
            ),
        ),
    )


def _store_form() -> Form:
    return Form(
        title="Store a secret",
        intro="The value is written straight to the store; nothing echoes it back.",
        submit_label="Store",
        fields=(
            FormField(name=_F_NAME, label="Name", type="text", placeholder="github-token"),
            FormField(name=_F_VALUE, label="Value", type="password"),
        ),
    )


def _policy_form(name: str, policy: dict[str, Any]) -> Form:
    """Who may reach a secret, and on what terms — every field at once.

    All of it in one modal rather than a field at a time: subjects and rules
    decide the same question from two directions, and editing one without seeing
    the other is how a policy ends up meaning something nobody intended.

    The same modal writes the FIRST policy as well as a change to one, because
    the alternative is a surface where a secret can be stored and then never
    made to work — the machine holding `operator.key` back in the loop for the
    one step that decides whether any of this was any use. What changes is what
    an empty field means: nothing to leave, so it takes the default shown.
    """
    creating = not policy
    policy = policy or _NEW_POLICY
    grant = int(policy.get("max_grant_s") or 0)
    return Form(
        title=f"Policy for {name}"[:_TITLE_MAX],
        intro=(
            "No policy yet, so no agent can reach this. Name the agents that "
            "may ask; anything left empty takes the value shown."
            if creating else
            "Empty fields are left as they are."
        ),
        submit_label="Create" if creating else "Save",
        fields=(
            FormField(
                name=_F_SUBJECTS, label="Agents that may ask (comma separated)",
                type="text",
                # Required when there is nothing to leave alone: a policy naming
                # nobody is exactly as unreachable as no policy, so asking for it
                # here beats accepting it and refusing after the submit.
                optional=not creating,
                placeholder=str(policy.get("subjects") or "nobody yet"),
            ),
            FormField(
                name=_F_APPROVAL, label="Approval", type="select", optional=True,
                options=APPROVALS,
                placeholder=str(policy.get("approval") or APPROVAL_ALWAYS),
            ),
            FormField(
                name=_F_GRANT, label="Longest window a human may open",
                type="select", optional=True,
                # Seconds as the operator reads them; parsed back on submit. A
                # select spares them the "is 900 minutes or seconds" question
                # that a free-text duration always raises.
                options=("none", *(humanize(s) for s in GRANT_LADDER)),
                placeholder=humanize(grant) if grant else "none",
            ),
            FormField(
                name=_F_RULES,
                label="Automatic for these commands (one per line)",
                type="textarea", optional=True,
                placeholder="\n".join(policy.get("auto_commands") or []) or "none",
                help_text=(
                    "A trailing * means 'and any arguments'. This is what the "
                    "caller SAYS it will run — see docs/secrets.md."
                ),
            ),
        ),
    )


def _seconds(chosen: str, current: int) -> int:
    """The window a select returned, back to seconds."""
    chosen = chosen.strip()
    if not chosen:
        return current
    if chosen == "none":
        return 0
    for value in GRANT_LADDER:
        if humanize(value) == chosen:
            return value
    return current


def _diff(current: dict[str, Any], subjects: str, approval: str, grant: int,
          rules: list[str]) -> str:
    """What actually changed, in the words the operator used."""
    was_rules = list(current.get("auto_commands") or [])
    parts = []
    if subjects != current["subjects"]:
        parts.append(f"subjects {current['subjects'] or '(nobody)'} → {subjects}")
    if approval != current["approval"]:
        parts.append(f"approval {current['approval']} → {approval}")
    if grant != int(current["max_grant_s"] or 0):
        parts.append(f"window {current['max_grant_s']}s → {grant}s")
    if rules != was_rules:
        parts.append(f"rules {len(was_rules)} → {len(rules)}: {'; '.join(rules) or 'none'}")
    return ", ".join(parts)


def _capped_note(hidden: int) -> str:
    return f"_…and {hidden} more — `impi ward ls` shows them all._"


class PendingOperatorForms:
    """Which of the modals a token is — unlock, unseal, store.

    Only the verb: WHOSE form it is comes from the record the receiver routed
    on, and WHO answered it comes from the submission. In memory on purpose — a
    form that outlives a restart of the broker is a form whose answer nobody is
    waiting for.
    """

    def __init__(self) -> None:
        self._open: dict[str, str] = {}  # token -> verb

    def register(self, token: str, verb: str) -> None:
        self._open[token] = verb

    def take(self, token: str) -> str:
        return self._open.pop(token, "")


class OperatorForms:
    """What the modals do when they come back.

    Implements ``FormHandler``: the values are the application's, and the one
    thing that must not happen to them is becoming a message in a conversation.
    An exception raised here is logged by the dispatcher and the values are
    dropped — never handed to anybody else.
    """

    def __init__(
        self,
        broker: SecretLeasing,
        operations: Operations,
        approvers: Approvers,
        admin: ChatAdmin,
        chat: ChatClient,
        ledger: ApprovalStore,
        pending: PendingOperatorForms,
    ) -> None:
        self._broker = broker
        self._ops = operations
        self._approvers = approvers
        self._admin = admin
        self._chat = chat
        self._ledger = ledger
        self._pending = pending

    async def handle(
        self, record: FormRecord, values: Mapping[str, str], user_id: str
    ) -> None:
        """Answer one of the operator modals.

        Reached only for a form written with this handler's name, so there is no
        ownership to establish — what is left is whether the person who filled
        it in may act. Re-checked here rather than inherited from the click that
        opened the dialog: the submission arrives from the platform, and says
        for itself who sent it.
        """
        verb = self._pending.take(record.token)
        if not verb:
            # The broker restarted between opening the modal and answering it,
            # so nothing knows what this form was for.
            logger.warning("ward: a form came back that this process did not open")
            await self._tell(user_id, "🔴 That form is no longer open — run `/ward` again.")
            return
        if not await self._approvers.allows(user_id):
            logger.warning("ward %s: submission from a non-operator", verb)
            await self._tell(user_id, _NOT_YOURS)
            return
        try:
            text = await self._perform(verb, values, user_id)
        except SecretBackendError as exc:
            text = f"🔴 {one_line(str(exc))}"
        await self._tell(user_id, text)

    async def _perform(self, verb: str, values: Mapping[str, str], user_id: str) -> str:
        if verb in ("unlock", "unseal"):
            return await self._open_store(verb, values, user_id)
        if verb == "store":
            return await self._store(values, user_id)
        if verb.startswith(f"{_POLICY}:"):
            return await self._policy(verb.split(":", 1)[1], values, user_id)
        logger.warning("ward: unknown operator form %r", verb)
        return "🔴 Unknown form."

    async def _open_store(
        self, verb: str, values: Mapping[str, str], user_id: str
    ) -> str:
        material = UnlockMaterial(
            unseal_key=values.get(_F_UNSEAL, "").strip(),
            auth_secret=values.get(_F_SECRET_ID, "").strip(),
        )
        if not material:
            return "🔴 Nothing was filled in."
        state = await self._broker.unlock(material)
        await self._record(user_id, verb, "store")
        if not state.usable:
            return f"🔴 The store is still not usable — {one_line(state.detail or 'sealed')}"
        if material.unseal_key:
            # The credential can be replaced; the unseal key cannot, so the
            # reminder names the one thing that is still actionable.
            return (
                "🔓 The store is open.\n\nBoth values passed through this chat "
                "platform. The credential can be replaced — run `impi ward rotate` "
                "when you are back at the machine, and put the new one in "
                "`ward-recovery.txt`."
            )
        return (
            "🔓 The store is open.\n\nThe credential passed through this chat "
            "platform; `impi ward rotate` replaces it when you are next at the "
            "machine."
        )

    async def _store(self, values: Mapping[str, str], user_id: str) -> str:
        name = values.get(_F_NAME, "").strip()
        value = values.get(_F_VALUE, "")
        if not name or not value:
            return "🔴 Both a name and a value are needed."
        await self._ops.put_secret(name, {"value": value})
        await self._record(user_id, "set", name)
        known = {
            p["name"] for p in (await self._ops.list_policies()).get("policies") or []
        }
        stored = f"✅ `{one_line(name)}` is stored."
        if name in known:
            return stored
        # The chat path first, because that is the one available to whoever is
        # reading this: a message that named only the CLI would send them back
        # to the machine this surface exists to do without.
        return (
            f"{stored}\n\nIt has no policy, so no agent can reach it yet — "
            f"**Secrets** → **Set policy**, or at the machine:\n"
            f"`impi ward policy set {one_line(name)} --subjects <agent>`"
        )


    async def _policy(
        self, name: str, values: Mapping[str, str], user_id: str
    ) -> str:
        """Write a secret's policy from the form — its first one, or a change.

        When one exists, empty means "leave it": a modal cannot tell "cleared"
        from "not touched", and losing a subject list to an untouched field would
        be the worse reading of the two. Clearing is `impi ward policy set` with
        the flag that says so.

        When none does, there is nothing to leave, so empty takes the default the
        modal showed — except the agents. A policy naming nobody is as unreachable
        as no policy at all, so that one is asked for rather than defaulted. The
        modal marks it required; this checks again, because a submission arrives
        from the platform and says for itself what is in it.
        """
        answer = await self._ops.list_policies()
        found = next(
            (p for p in answer.get("policies") or [] if p["name"] == name), None
        )
        creating = found is None
        current = dict(_NEW_POLICY) if found is None else found

        subjects = values.get(_F_SUBJECTS, "").strip() or str(current["subjects"])
        if not subjects and creating:
            return (
                f"🔴 `{one_line(name)}` has no policy yet, and one naming no agent "
                "would leave it just as unreachable. Name at least one."
            )
        approval = values.get(_F_APPROVAL, "").strip() or str(current["approval"])
        grant = _seconds(values.get(_F_GRANT, ""), int(current["max_grant_s"] or 0))
        written = values.get(_F_RULES, "").strip()
        rules = (
            [line.strip() for line in written.splitlines() if line.strip()]
            if written else list(current.get("auto_commands") or [])
        )
        try:
            for line in rules:
                parse(line)  # refuse here, so a bad rule never reaches the store
        except RuleError as exc:
            return f"🔴 {one_line(str(exc))}\n\nThe policy was not changed."

        await self._ops.put_policy(name, {
            "approval": approval, "max_grant_s": grant, "subjects": subjects,
            "description": current.get("description", ""), "auto_commands": rules,
        })
        # The diff, not just "changed": handing an agent access to a secret from
        # a chat session has to be answerable afterwards, and "who did what" is
        # the whole of that answer.
        changes = _diff(current, subjects, approval, grant, rules)
        made = "created" if creating else "saved"
        await self._record(
            user_id, "policy",
            f"{name}: {'created, ' if creating else ''}{changes or 'no change'}",
        )
        return (
            f"✅ Policy for `{one_line(name)}` {made}.\n"
            f"{one_line(changes) if changes else 'Nothing changed.'}"
        )

    async def _tell(self, user_id: str, text: str) -> None:
        """The outcome goes to the operator's own direct message, wherever the
        modal was opened from — it is an answer to a person, not to a room."""
        try:
            channel = await self._admin.open_direct(user_id)
            if not channel:
                return
            await self._chat.post_reply(
                ConversationRef(
                    channel_id=channel, conversation_id=channel, message_id=channel
                ),
                text,
            )
        except Exception:
            logger.warning("ward: could not report the outcome", exc_info=True)

    async def _record(self, user_id: str, verb: str, scope: str) -> None:
        await _record_operator(self._ledger, user_id, verb, scope)


async def _record_operator(
    ledger: ApprovalStore, user_id: str, verb: str, scope: str
) -> None:
    """One ledger row per operator action taken from chat.

    Without it "who unsealed the store at three in the morning" has no answer:
    the CLI path is at least a shell somebody owns, while a chat session is a
    session, and the ledger is the only place the two look the same.
    """
    await ledger.record_decision(
        ApprovalAudit(
            id=f"op_{tokens.token_hex(8)}",
            at=_now(),
            kind=KIND_OPERATOR,
            principal=user_id,
            scope=scope,
            reason="from chat",
            detail=verb,
            decision=verb,
            approver=user_id,
            grant_id="",
            request_id="",
            duration_ms=0,
        )
    )
