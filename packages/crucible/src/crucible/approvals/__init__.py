"""Asking a human to authorize something, and remembering that they did.

A general primitive, not a secrets one. Two consumers already: a credential an
agent asks for, and a tool call the runtime gates on. They differ in who may
answer and in what a yes leaves behind, which are parameters here rather than
two mechanisms.

The rendering half is the security-critical one: what a card says is all a human
has to go on, and everything interesting in it comes from the caller.
"""

from crucible.approvals.card import (
    CardLayout,
    card_layout,
    card_markdown,
    card_rows,
    plain_markdown,
    render_card,
)
from crucible.approvals.controls import (
    GRANT_LADDER,
    approval_actions,
    humanize,
    verdict_of,
    windows,
)
from crucible.approvals.pending import (
    ANSWER_DENY,
    ANSWER_GRANT_PREFIX,
    ANSWER_ONCE,
    APPROVAL_KEY,
    Approval,
    ApprovalOutcome,
    PendingApprovals,
    decide,
)
from crucible.approvals.ports import ConfirmOutcome, NotAsked

__all__ = [
    "ANSWER_DENY",
    "ANSWER_GRANT_PREFIX",
    "ANSWER_ONCE",
    "APPROVAL_KEY",
    "GRANT_LADDER",
    "Approval",
    "ApprovalOutcome",
    "ConfirmOutcome",
    "NotAsked",
    "PendingApprovals",
    "approval_actions",
    "CardLayout",
    "card_layout",
    "plain_markdown",
    "card_markdown",
    "card_rows",
    "decide",
    "humanize",
    "render_card",
    "verdict_of",
    "windows",
]
