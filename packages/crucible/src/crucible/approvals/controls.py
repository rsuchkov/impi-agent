"""The three answers a human can give, as controls on a card.

Allow once, allow for a while, deny. The ladder of windows is filtered by
whoever is asking — a policy's ceiling, a deployment's ceiling — so a card never
offers a duration that would be refused when clicked.
"""

from crucible.approvals.pending import (
    ANSWER_DENY,
    ANSWER_GRANT_PREFIX,
    ANSWER_ONCE,
    APPROVAL_KEY,
)
from crucible.ports.chat.types import ACTION_SELECT, Action, Choice

# An hour is the coarsest useful answer and a minute the finest: below that the
# window closes before the thing it was opened for finishes, and above it the
# window outlives the reason anyone remembers giving it.
GRANT_LADDER = (60, 300, 900, 3600)

_ALLOW_ONCE_LABEL = "Allow once"
_DENY_LABEL = "Deny"
_GRANT_PLACEHOLDER = "Allow for…"


def windows(ladder: tuple[int, ...] = GRANT_LADDER, *, ceiling_s: int = 0) -> tuple[int, ...]:
    """The window lengths worth offering, longest last. Empty when no window is
    allowed at all, which is how a card ends up with no dropdown rather than a
    dropdown whose every choice would be capped to nothing."""
    if ceiling_s <= 0:
        return ()
    return tuple(seconds for seconds in ladder if seconds <= ceiling_s)


def humanize(seconds: int) -> str:
    """A duration as a human would say it — the dropdown's labels, and the line
    the log shows an operator afterwards."""
    if seconds % 3600 == 0 and seconds >= 3600:
        hours = seconds // 3600
        return f"{hours} hour" if hours == 1 else f"{hours} hours"
    if seconds % 60 == 0 and seconds >= 60:
        return f"{seconds // 60} min"
    return f"{seconds}s"


def approval_actions(token: str, *, offers: tuple[int, ...] = ()) -> list[Action]:
    """Allow once, optionally allow for a while, deny."""
    context = {APPROVAL_KEY: token}
    actions = [
        Action(
            id="once", label=_ALLOW_ONCE_LABEL, value=ANSWER_ONCE,
            style="primary", context=context,
        )
    ]
    if offers:
        actions.append(
            Action(
                id="grant", label=_GRANT_PLACEHOLDER, kind=ACTION_SELECT,
                options=tuple(
                    Choice(label=humanize(seconds), value=f"{ANSWER_GRANT_PREFIX}{seconds}")
                    for seconds in offers
                ),
                context=context,
            )
        )
    actions.append(
        Action(id="deny", label=_DENY_LABEL, value=ANSWER_DENY, style="danger", context=context)
    )
    return actions
