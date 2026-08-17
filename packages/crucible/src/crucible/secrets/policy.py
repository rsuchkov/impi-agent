"""What a policy permits, decided without touching a store or a network.

Two questions live here. Whether an agent may ask for a secret at all, and — if
a human is going to be asked — how long a window they may be offered. Both are
pure functions of the policy record, which is what makes the authorization rules
cheap to test exhaustively.
"""

from dataclasses import dataclass

from crucible.store.base import (
    APPROVAL_NEVER,
    DECISION_AUTO,
    DECISION_NO_POLICY,
    DECISION_NOT_PERMITTED,
    SecretPolicyRecord,
)

# What a human may be offered when they decide to leave access open for a while.
# An hour is the coarsest useful answer and a minute the finest: below that the
# window closes before the command it was opened for finishes, and above it the
# grant outlives the reason anyone remembers giving it.
GRANT_LADDER = (60, 300, 900, 3600)

# The three shapes a verdict takes before anything is asked or read.
ASK = "ask"
ALLOW = "allow"
REFUSE = "refuse"


@dataclass(frozen=True)
class Verdict:
    """``outcome`` drives the broker; ``decision`` is what the ledger records
    when the verdict is final on its own (a refusal, or a policy that needs no
    human). When the outcome is ASK the decision depends on the answer."""

    outcome: str
    decision: str = ""


def evaluate(policy: SecretPolicyRecord | None, agent: str) -> Verdict:
    """Whether ``agent`` may reach the secret ``policy`` describes.

    The two refusals are separate values on purpose — the ledger needs to tell
    "nobody configured that name" from "that agent isn't on the list" — but the
    caller is told the same thing for both, which is the broker's job, not this
    function's.
    """
    if policy is None:
        return Verdict(REFUSE, DECISION_NO_POLICY)
    if not policy.allows(agent):
        return Verdict(REFUSE, DECISION_NOT_PERMITTED)
    if policy.approval == APPROVAL_NEVER:
        return Verdict(ALLOW, DECISION_AUTO)
    return Verdict(ASK)


def grant_options(policy: SecretPolicyRecord, *, ceiling_s: int = 0) -> tuple[int, ...]:
    """The window lengths a human may choose for this secret, longest last.

    Empty when no window is allowed at all — a secret with ``max_grant_s`` of 0
    is asked about every single time, and the approval card shows no dropdown
    rather than a dropdown that would be refused on submit.
    """
    cap = policy.max_grant_s
    if ceiling_s > 0:
        cap = min(cap, ceiling_s)
    return tuple(seconds for seconds in GRANT_LADDER if seconds <= cap)
