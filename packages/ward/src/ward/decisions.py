"""How a request for a credential came out, in the ledger's own words.

The engine's approval primitive already names what a human did — allowed once,
allowed for a while, denied, nobody answered. What it cannot name is anything
about *secrets*: no policy under that name, an agent not on the list, a store
that is sealed. Those are this broker's business, so they are spelled here and
the engine never learns them.

Closed set on purpose. The caller is told the same thing whatever happened —
that is the point of the refusals being indistinguishable — so "why did that not
work" has to be greppable in the ledger or it is nowhere.
"""

from crucible.store.base import (
    DECISION_APPROVED_GRANT,
    DECISION_APPROVED_ONCE,
    DECISION_DENIED,
    DECISION_NO_APPROVER,
    DECISION_REUSED_GRANT,
    DECISION_TIMEOUT,
)

# What a window and a ledger row are about here. The column is a plain string
# precisely so an application can bring its own word for what it authorizes.
KIND_SECRET = "secret"
# And what an operator did, when they did it from chat rather than from the CLI.
# The ledger is the only place those two look alike: a shell is a machine
# somebody owns, a chat session is a session, and "who unsealed the store at
# three in the morning" has to be answerable for both.
KIND_OPERATOR = "operator"

DECISION_AUTO = "auto"  # policy says approval: never
DECISION_NO_POLICY = "no_policy"  # nothing is configured under that name
DECISION_NOT_PERMITTED = "not_permitted"  # the policy does not list this agent
DECISION_NOT_REACHED = "not_reached"  # its request died on another of its secrets
DECISION_LOCKED = "locked"  # the broker has no backend credential yet
DECISION_SEALED = "sealed"  # the backend itself is sealed
DECISION_BACKEND_ERROR = "backend_error"  # approved, but the read failed

# Everything a lease can be recorded as: the engine's six, plus this broker's own.
DECISIONS = (
    DECISION_APPROVED_ONCE,
    DECISION_APPROVED_GRANT,
    DECISION_REUSED_GRANT,
    DECISION_DENIED,
    DECISION_TIMEOUT,
    DECISION_NO_APPROVER,
    DECISION_AUTO,
    DECISION_NO_POLICY,
    DECISION_NOT_PERMITTED,
    DECISION_NOT_REACHED,
    DECISION_LOCKED,
    DECISION_SEALED,
    DECISION_BACKEND_ERROR,
)

# The three that mean "nobody could be served right now" rather than "you may
# not": an operator's problem, and the one thing a caller is allowed to tell
# apart, because it says nothing about what exists.
UNAVAILABLE_DECISIONS = frozenset({DECISION_LOCKED, DECISION_SEALED, DECISION_BACKEND_ERROR})
