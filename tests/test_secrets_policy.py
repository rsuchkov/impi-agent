"""Who a policy permits, and how long a window it allows (secrets/policy.py).

Pure functions, so this is the cheap place to be exhaustive about the
authorization rules — which is the point of keeping them out of the broker.
"""

from crucible.secrets.approvals import humanize
from crucible.secrets.policy import (
    ALLOW,
    ASK,
    GRANT_LADDER,
    REFUSE,
    evaluate,
    grant_options,
)
from crucible.store.base import (
    APPROVAL_ALWAYS,
    APPROVAL_NEVER,
    DECISION_AUTO,
    DECISION_NO_POLICY,
    DECISION_NOT_PERMITTED,
    SecretPolicyRecord,
)

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
    assert grant_options(_policy(max_grant_s=3600)) == GRANT_LADDER
    assert grant_options(_policy(max_grant_s=900)) == (60, 300, 900)
    assert grant_options(_policy(max_grant_s=300)) == (60, 300)
    assert grant_options(_policy(max_grant_s=90)) == (60,)


def test_a_secret_that_allows_no_window_offers_none() -> None:
    # The card then shows only "allow once" and "deny" — a dropdown whose every
    # choice would be capped to nothing is worse than no dropdown.
    assert grant_options(_policy(max_grant_s=0)) == ()
    assert grant_options(_policy(max_grant_s=59)) == ()


def test_the_deployment_ceiling_applies_on_top_of_the_policy() -> None:
    assert grant_options(_policy(max_grant_s=3600), ceiling_s=300) == (60, 300)
    # And never widens what the policy allows.
    assert grant_options(_policy(max_grant_s=300), ceiling_s=3600) == (60, 300)


def test_durations_read_the_way_a_person_says_them() -> None:
    assert humanize(60) == "1 min"
    assert humanize(300) == "5 min"
    assert humanize(900) == "15 min"
    assert humanize(3600) == "1 hour"
    assert humanize(7200) == "2 hours"
    assert humanize(45) == "45s"
