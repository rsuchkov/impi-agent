"""Requests waiting on a human, and the answers they come back with.

One registry for everything a human authorizes. Two consumers today — a
credential an agent asked for, and a tool call it wants to make — and they
differ in two parameters, not in kind:

* **who may answer.** A named approver for a credential; anyone in the
  conversation for a tool call, which is the behaviour the blocking confirm has
  always had. An empty approver set means the latter.
* **what a yes leaves behind.** "Once" and "for fifteen minutes" differ in
  whether a window is opened, so the answer carries a duration rather than a
  boolean.

In memory on purpose, like ``interactions/pending_ui.py``: a pending request
lives inside one caller's blocked call, and there is nothing to resume if the
process restarts under it.
"""

import asyncio
import logging
from dataclasses import dataclass
from enum import Enum, auto

logger = logging.getLogger(__name__)

# Marks a click as an answer to a request for authorization, and carries which
# one. It rides in the action context, so the receiver routes without a store
# lookup. Distinct from the widget/pending-UI token: that one is answered by the
# conversation and leaves nothing behind.
APPROVAL_KEY = "approval"

# The values the controls send back.
ANSWER_ONCE = "once"
ANSWER_DENY = "deny"
ANSWER_GRANT_PREFIX = "grant:"


@dataclass(frozen=True)
class Approval:
    """What a human decided, or what their silence decided for them."""

    allowed: bool
    grant_s: int = 0  # 0 = this call only, no window left behind
    approver: str = ""  # the user id that decided; "" when nobody did
    timed_out: bool = False


class ApprovalOutcome(Enum):
    RESOLVED = auto()  # the answer was accepted and the caller is unblocked
    NOT_MINE = auto()  # no such pending request (try the other click handlers)
    NOT_ALLOWED = auto()  # a live request, but this user may not answer it


@dataclass
class _Pending:
    future: "asyncio.Future[Approval]"
    kind: str
    principal: str
    scopes: tuple[str, ...]
    approvers: frozenset[str]  # empty = whoever is in the conversation


class PendingApprovals:
    """The requests currently waiting on a human."""

    def __init__(self) -> None:
        self._by_token: dict[str, _Pending] = {}

    def register(
        self,
        token: str,
        *,
        kind: str,
        principal: str,
        scopes: tuple[str, ...],
        approvers: frozenset[str] = frozenset(),
    ) -> "asyncio.Future[Approval]":
        """Start waiting. The approver set is captured per request rather than
        read at resolve time, so editing the configuration mid-flight cannot
        widen a question that was already asked."""
        future: asyncio.Future[Approval] = asyncio.get_running_loop().create_future()
        self._by_token[token] = _Pending(future, kind, principal, scopes, approvers)
        return future

    def discard(self, token: str) -> None:
        """Drop a token without resolving — the post failed, or the wait timed
        out and the caller has already given up."""
        self._by_token.pop(token, None)

    def pending(self, token: str) -> bool:
        return token in self._by_token

    def resolve(self, token: str, value: str, user_id: str) -> ApprovalOutcome:
        """Answer a waiting request from a click."""
        entry = self._by_token.get(token)
        if entry is None:
            return ApprovalOutcome.NOT_MINE
        if entry.approvers and user_id not in entry.approvers:
            logger.warning(
                "approval %s (%s): %s may not answer for %s (%s)",
                token[:8], entry.kind, user_id or "an anonymous click", entry.principal,
                ", ".join(entry.scopes),
            )
            return ApprovalOutcome.NOT_ALLOWED
        self._by_token.pop(token, None)
        if entry.future.done():
            return ApprovalOutcome.NOT_MINE
        entry.future.set_result(decide(value, user_id))
        return ApprovalOutcome.RESOLVED


def decide(value: str, user_id: str) -> Approval:
    """Decode a control's value. Anything unrecognized is a refusal: a malformed
    payload must never be the thing that authorizes something."""
    if value == ANSWER_ONCE:
        return Approval(allowed=True, approver=user_id)
    if value.startswith(ANSWER_GRANT_PREFIX):
        try:
            seconds = int(value[len(ANSWER_GRANT_PREFIX) :])
        except ValueError:
            return Approval(allowed=False, approver=user_id)
        return Approval(allowed=seconds > 0, grant_s=max(seconds, 0), approver=user_id)
    return Approval(allowed=False, approver=user_id)
