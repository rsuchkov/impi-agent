"""What a policy permits, decided without touching a store or a network.

Two questions live here. Whether an agent may ask for a secret at all, and — if
a human is going to be asked — how long a window they may be offered. Both are
pure functions of the policy record, which is what makes the authorization rules
cheap to test exhaustively.
"""

from dataclasses import dataclass

from ward.decisions import (
    DECISION_AUTO,
    DECISION_NO_POLICY,
    DECISION_NOT_PERMITTED,
)
from ward.store import SecretPolicyRecord
from wardline.wire import APPROVAL_NEVER

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


def ceiling(policy: SecretPolicyRecord | None, *, deployment_s: int = 0) -> int:
    """How long a window over this secret may stay open.

    The policy's own ceiling, never above the deployment's. Zero means no window
    at all: every single use is asked about, and the card shows no dropdown
    rather than one whose choices would be capped to nothing.
    """
    if policy is None:
        return 0
    if deployment_s > 0:
        return min(policy.max_grant_s, deployment_s)
    return policy.max_grant_s
