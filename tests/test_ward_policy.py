"""Who a policy permits, and how long a window it allows (ward/policy.py).

Pure functions, so this is the cheap place to be exhaustive about the
authorization rules — which is the point of keeping them out of the broker.
"""

from crucible.approvals import GRANT_LADDER, humanize, windows
from ward.decisions import (
    DECISION_AUTO,
    DECISION_NO_POLICY,
    DECISION_NOT_PERMITTED,
)
from ward.policy import ALLOW, ASK, REFUSE, ceiling, evaluate
from ward.store import SecretPolicyRecord
from wardline.wire import APPROVAL_ALWAYS, APPROVAL_NEVER

T0 = "2026-08-11T09:00:00+00:00"


def _policy(**over) -> SecretPolicyRecord:
    base = dict(
        name="github-token", approval=APPROVAL_ALWAYS, max_grant_s=3600,
        subjects="assistant", description="", created_at=T0, updated_at=T0,
    )
    base.update(over)
    return SecretPolicyRecord(**base)  # type: ignore[arg-type]


def test_a_secret_nobody_configured_is_reachable_by_nobody() -> None:
    verdict = evaluate(None, "assistant")
    assert (verdict.outcome, verdict.decision) == (REFUSE, DECISION_NO_POLICY)


def test_an_agent_off_the_list_is_refused() -> None:
    verdict = evaluate(_policy(subjects="builder"), "assistant")
    assert (verdict.outcome, verdict.decision) == (REFUSE, DECISION_NOT_PERMITTED)


def test_a_policy_with_no_subjects_permits_nobody() -> None:
    # The allowlist starts empty on purpose: a new secret is reachable only
    # after somebody says who may reach it.
    assert evaluate(_policy(subjects=""), "assistant").outcome == REFUSE


def test_approval_never_serves_without_asking() -> None:
    verdict = evaluate(_policy(approval=APPROVAL_NEVER), "assistant")
    assert (verdict.outcome, verdict.decision) == (ALLOW, DECISION_AUTO)


def test_the_ordinary_case_asks() -> None:
    assert evaluate(_policy(), "assistant").outcome == ASK


def test_the_window_ladder_stops_at_the_policy_ceiling() -> None:
    assert windows(ceiling_s=ceiling(_policy(max_grant_s=3600))) == GRANT_LADDER
    assert windows(ceiling_s=ceiling(_policy(max_grant_s=900))) == (60, 300, 900)
    assert windows(ceiling_s=ceiling(_policy(max_grant_s=90))) == (60,)


def test_a_secret_that_allows_no_window_offers_none() -> None:
    # The card then shows only "allow once" and "deny" — a dropdown whose every
    # choice would be capped to nothing is worse than no dropdown.
    assert windows(ceiling_s=ceiling(_policy(max_grant_s=0))) == ()
    assert windows(ceiling_s=ceiling(_policy(max_grant_s=59))) == ()


def test_the_deployment_ceiling_applies_on_top_of_the_policy() -> None:
    assert ceiling(_policy(max_grant_s=3600), deployment_s=300) == 300
    # And never widens what the policy allows.
    assert ceiling(_policy(max_grant_s=300), deployment_s=3600) == 300


def test_a_secret_with_no_policy_allows_no_window() -> None:
    assert ceiling(None, deployment_s=3600) == 0


def test_durations_read_the_way_a_person_says_them() -> None:
    assert humanize(60) == "1 min"
    assert humanize(300) == "5 min"
    assert humanize(900) == "15 min"
    assert humanize(3600) == "1 hour"
    assert humanize(7200) == "2 hours"
    assert humanize(45) == "45s"


def test_the_brokers_decision_vocabulary_is_closed_and_complete() -> None:
    """The library tests its own six the same way. This is the other half: a
    decision the broker can record and forgot to list is a decision nobody can
    grep for, and the ledger is the only place a real reason is ever written."""
    from ward import decisions

    named = {
        value
        for name, value in vars(decisions).items()
        if name.startswith("DECISION_") and isinstance(value, str)
    }
    assert set(decisions.DECISIONS) == named
    assert len(decisions.DECISIONS) == len(named)  # no duplicates either
    # And the three that mean "not right now" are decisions, not typos.
    assert decisions.UNAVAILABLE_DECISIONS <= set(decisions.DECISIONS)
