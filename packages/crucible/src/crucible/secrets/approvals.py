"""Outstanding requests for a human's approval, and the card they answer.

Modelled on ``interactions/pending_ui.py`` — a one-shot ``token -> Future`` that
a click resolves — with two differences that matter.

**The clicker is checked.** The existing blocking-UI registry resolves on any
click that carries the token, because the question it asks ("shall I go ahead?")
is addressed to whoever is in the conversation. A request for a credential is
addressed to a named person, so a click from anyone else is refused and logged
rather than honoured.

**The answer is a duration, not a yes.** "Allow once" and "allow for fifteen
minutes" differ in what they leave behind, so the outcome carries the window
length the human picked.

In memory, like its model: a pending approval lives inside one ``secret-exec``
HTTP request, and there is nothing to resume if the engine restarts under it.

This module deliberately imports nothing but the chat vocabulary. Both gateways
route the click that answers an approval, and the gateway layer may not reach
the store — so the decode side of this contract has to be reachable from there.
"""

import asyncio
import logging
from dataclasses import dataclass
from enum import Enum, auto

from crucible.approvals.card import command_line, render_card
from crucible.ports.chat.types import ACTION_SELECT, Action, Choice

logger = logging.getLogger(__name__)

# Marks a click as an answer to a secret request, and carries which one. It
# rides in the action context, so the receiver can route without a store lookup.
SECRET_APPROVAL_KEY = "secret_approval"

# The values the controls send back.
ANSWER_ONCE = "once"
ANSWER_DENY = "deny"
ANSWER_GRANT_PREFIX = "grant:"

_ALLOW_ONCE_LABEL = "Allow once"
_DENY_LABEL = "Deny"
_GRANT_PLACEHOLDER = "Allow for…"


@dataclass(frozen=True)
class Approval:
    """What a human decided, or what their silence decided for them."""

    allowed: bool
    grant_s: int = 0  # 0 = this call only, no window left behind
    approver: str = ""  # the user id that decided; "" when nobody did
    timed_out: bool = False


class SecretApprovalOutcome(Enum):
    RESOLVED = auto()  # the answer was accepted and the caller is unblocked
    NOT_MINE = auto()  # no such pending approval (try the other click handlers)
    NOT_ALLOWED = auto()  # a live approval, but this user may not answer it


@dataclass
class _Pending:
    future: "asyncio.Future[Approval]"
    agent: str
    secrets: tuple[str, ...]
    approvers: frozenset[str]


class SecretApprovals:
    """The requests currently waiting on a human."""

    def __init__(self) -> None:
        self._by_token: dict[str, _Pending] = {}

    def register(
        self, token: str, *, agent: str, secrets: tuple[str, ...], approvers: frozenset[str]
    ) -> "asyncio.Future[Approval]":
        """Start waiting. The approver set is captured per request rather than
        read at resolve time, so editing the configuration mid-flight cannot
        widen a question that was already asked."""
        future: asyncio.Future[Approval] = asyncio.get_running_loop().create_future()
        self._by_token[token] = _Pending(future, agent, secrets, approvers)
        return future

    def discard(self, token: str) -> None:
        """Drop a token without resolving — the post failed, or the wait timed
        out and the caller has already given up."""
        self._by_token.pop(token, None)

    def pending(self, token: str) -> bool:
        return token in self._by_token

    def resolve(self, token: str, value: str, user_id: str) -> SecretApprovalOutcome:
        """Answer a waiting request from a click."""
        entry = self._by_token.get(token)
        if entry is None:
            return SecretApprovalOutcome.NOT_MINE
        if user_id not in entry.approvers:
            logger.warning(
                "secret approval %s: %s may not answer for %s (%s)",
                token[:8], user_id or "an anonymous click", entry.agent,
                ", ".join(entry.secrets),
            )
            return SecretApprovalOutcome.NOT_ALLOWED
        self._by_token.pop(token, None)
        if entry.future.done():
            return SecretApprovalOutcome.NOT_MINE
        entry.future.set_result(_decide(value, user_id))
        return SecretApprovalOutcome.RESOLVED


def _decide(value: str, user_id: str) -> Approval:
    """Decode a control's value. Anything unrecognized is a refusal: a malformed
    payload must never be the thing that hands out a credential."""
    if value == ANSWER_ONCE:
        return Approval(allowed=True, approver=user_id)
    if value.startswith(ANSWER_GRANT_PREFIX):
        try:
            seconds = int(value[len(ANSWER_GRANT_PREFIX) :])
        except ValueError:
            return Approval(allowed=False, approver=user_id)
        return Approval(allowed=seconds > 0, grant_s=max(seconds, 0), approver=user_id)
    return Approval(allowed=False, approver=user_id)


def humanize(seconds: int) -> str:
    """A duration as a human would say it — the dropdown's labels, and the line
    the log shows an operator afterwards."""
    if seconds % 3600 == 0 and seconds >= 3600:
        hours = seconds // 3600
        return f"{hours} hour" if hours == 1 else f"{hours} hours"
    if seconds % 60 == 0 and seconds >= 60:
        return f"{seconds // 60} min"
    return f"{seconds}s"


def approval_text(
    agent: str, *, references: tuple[str, ...], reason: str, command: tuple[str, ...]
) -> str:
    """The message a human reads before deciding.

    Every value here except the agent's own name comes from the caller, so the
    card is assembled through ``render_card``, which lets a caller supply
    content and never structure — see the reasoning in ``approvals/card.py``.

    The command is not decoration. A caller allowed to bind a secret into a
    child process is also allowed to bind it into ``sh -c 'echo $TOKEN'``, so
    the argv shown here is the only thing standing between an approval and an
    exfiltration. It is also only a *claim*: nothing forces the caller to run
    what it said it would.
    """
    label = "Secret" if len(references) == 1 else "Secrets"
    return render_card(
        f"🔐 **{agent}** is asking for a secret.",
        [(label, ", ".join(references)), ("Reason", reason)],
        block_label="Command",
        block=command_line(command),
    )


def approval_actions(token: str, *, windows: tuple[int, ...]) -> list[Action]:
    """Allow once, optionally allow for a while, deny.

    The windows come from the policy, so a secret that permits no window shows
    no dropdown at all rather than one whose choices would be refused.
    """
    context = {SECRET_APPROVAL_KEY: token}
    actions = [
        Action(
            id="once", label=_ALLOW_ONCE_LABEL, value=ANSWER_ONCE,
            style="primary", context=context,
        )
    ]
    if windows:
        actions.append(
            Action(
                id="grant", label=_GRANT_PLACEHOLDER, kind=ACTION_SELECT,
                options=tuple(
                    Choice(label=humanize(seconds), value=f"{ANSWER_GRANT_PREFIX}{seconds}")
                    for seconds in windows
                ),
                context=context,
            )
        )
    actions.append(
        Action(id="deny", label=_DENY_LABEL, value=ANSWER_DENY, style="danger", context=context)
    )
    return actions
