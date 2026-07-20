from crucible.loopguard import LoopGuard


class FakeClock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


def test_allows_under_both_limits() -> None:
    guard = LoopGuard(max_hops=4, max_agent_turns=6, window_s=60.0, now=FakeClock())
    assert guard.check(conversation_id="c1", hop_depth=0).allow
    assert guard.check(conversation_id="c1", hop_depth=3).allow


def test_hop_cap_refuses_deep_turns() -> None:
    guard = LoopGuard(max_hops=4, now=FakeClock())
    d = guard.check(conversation_id="c1", hop_depth=4)
    assert not d.allow
    assert "hop depth" in d.reason


def test_hop_cap_refusal_does_not_consume_rate_budget() -> None:
    # A hop-capped turn must not eat the rate window (it never ran).
    guard = LoopGuard(max_hops=1, max_agent_turns=2, window_s=60.0, now=FakeClock())
    assert not guard.check(conversation_id="c1", hop_depth=5).allow
    assert not guard.check(conversation_id="c1", hop_depth=5).allow
    # depth 0 still has its full budget of 2
    assert guard.check(conversation_id="c1", hop_depth=0).allow
    assert guard.check(conversation_id="c1", hop_depth=0).allow
    assert not guard.check(conversation_id="c1", hop_depth=0).allow


def test_rate_limit_trips_and_recovers_after_window() -> None:
    clock = FakeClock()
    guard = LoopGuard(max_hops=99, max_agent_turns=2, window_s=60.0, now=clock)
    assert guard.check(conversation_id="c1", hop_depth=0).allow
    assert guard.check(conversation_id="c1", hop_depth=0).allow
    tripped = guard.check(conversation_id="c1", hop_depth=0)
    assert not tripped.allow and "rate limit" in tripped.reason

    clock.t = 61.0  # window slides past the first two turns
    assert guard.check(conversation_id="c1", hop_depth=0).allow


def test_rate_limit_is_per_conversation() -> None:
    guard = LoopGuard(max_hops=99, max_agent_turns=1, window_s=60.0, now=FakeClock())
    assert guard.check(conversation_id="c1", hop_depth=0).allow
    assert not guard.check(conversation_id="c1", hop_depth=0).allow
    assert guard.check(conversation_id="c2", hop_depth=0).allow  # separate budget
