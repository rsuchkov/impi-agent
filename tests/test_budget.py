"""An agent's budget at the model: the day's spending read from the ledger
before a turn, the turn's own tokens held by the runtime, what every turn cost
written down."""

from datetime import datetime, timezone
from pathlib import Path

import pytest

from crucible.budget import NOTICE_BUDGET_SPENT, BudgetAdmission, day_start
from crucible.ports.admission import NOTICE_NOT_ADMITTED, NotAdmitted
from crucible.ports.agent.errors import NOTICE_TOKEN_BUDGET, AgentOverBudget, notice_for
from crucible.ports.agent.spec import Budget
from crucible.ports.chat.types import KIND_DM, ConversationRef, IncomingMessage
from crucible.ports.telemetry import BudgetHit
from crucible.store.base import Store, UsageRecord, UsageSum
from crucible.store.sqlite import SqliteSessionStore

NOON = datetime(2026, 11, 6, 12, 0, tzinfo=timezone.utc)


def _msg(user_id: str = "u1") -> IncomingMessage:
    return IncomingMessage(ref=ConversationRef("d1", "d1", "m1", ""), text="hi", user_id=user_id, kind=KIND_DM, is_dm=True, gateway="slack")


def _spent(agent: str, user_id: str, at: str, total: int, cost: float = 0.0) -> UsageRecord:
    return UsageRecord(agent=agent, user_id=user_id, at=at, total=total, cost_usd=cost)


class _Telemetry:
    def __init__(self) -> None:
        self.events: list = []

    def record(self, event) -> None:
        self.events.append(event)


# --- the ledger, on every backend --------------------------------------------------------


async def test_the_ledger_sums_an_agents_day_and_a_persons_and_forgets_by_age(store: Store) -> None:
    await store.add_usage(_spent("assistant", "u1", "2026-11-06T09:00:00+00:00", 100, 0.01))
    await store.add_usage(_spent("assistant", "u2", "2026-11-06T10:00:00+00:00", 50, 0.005))
    await store.add_usage(_spent("assistant", "u1", "2026-11-05T23:00:00+00:00", 999, 0.1))  # yesterday
    await store.add_usage(_spent("coder", "u1", "2026-11-06T11:00:00+00:00", 7))  # another agent
    since = "2026-11-06T00:00:00+00:00"
    assert await store.usage_since("assistant", since) == UsageSum(total=150, cost_usd=pytest.approx(0.015))  # type: ignore[comparison-overlap]
    assert await store.usage_since("assistant", since, user_id="u1") == UsageSum(total=100, cost_usd=pytest.approx(0.01))  # type: ignore[comparison-overlap]
    assert await store.usage_since("assistant", since, user_id="nobody") == UsageSum()
    assert await store.usage_since("nobody", since) == UsageSum()
    assert await store.prune_usage(before=since) == 1
    assert await store.usage_since("assistant", "2026-11-05T00:00:00+00:00") == UsageSum(total=150, cost_usd=pytest.approx(0.015))  # type: ignore[comparison-overlap]


# --- the rule ------------------------------------------------------------------------------


def test_the_day_starts_at_midnight_utc() -> None:
    assert day_start(datetime(2026, 11, 6, 23, 59, tzinfo=timezone.utc)) == "2026-11-06T00:00:00+00:00"


async def test_an_agent_and_a_person_are_held_to_the_days_budget_and_told_so(tmp_path: Path) -> None:
    store = SqliteSessionStore(tmp_path / "db.sqlite")
    telemetry = _Telemetry()
    try:
        await store.add_usage(_spent("assistant", "u1", "2026-11-06T09:00:00+00:00", 800, 0.08))
        await store.add_usage(_spent("assistant", "u2", "2026-11-06T09:30:00+00:00", 300, 0.03))
        await store.add_usage(_spent("assistant", "u1", "2026-11-05T09:00:00+00:00", 5000, 0.5))  # yesterday does not count
        budget = Budget(tokens_per_day=2000, usd_per_day=1.0, tokens_per_day_per_person=1000, usd_per_day_per_person=0.5)
        rule = BudgetAdmission(store, lambda agent: budget if agent == "assistant" else Budget(), telemetry=telemetry, now=lambda: NOON)
        assert await rule.admit("assistant", _msg("u1")) is None  # 1100 of 2000 for the agent, 800 of 1000 for u1
        assert await rule.admit("coder", _msg("u1")) is None  # no budget: admitted
        await store.add_usage(_spent("assistant", "u1", "2026-11-06T10:00:00+00:00", 200, 0.02))
        verdict = await rule.admit("assistant", _msg("u1"))
        assert verdict == NotAdmitted(
            text="Your budget with this agent for today is spent (1000 of 1000 tokens); ask again after 00:00 UTC.",
            code=NOTICE_BUDGET_SPENT,
        )
        assert await rule.admit("assistant", _msg("u2")) is None  # another person: their own count
        assert telemetry.events == [BudgetHit(agent="assistant", user_id="u1", rule="tokens_per_day_per_person", spent=1000, limit=1000)]
        await store.add_usage(_spent("assistant", "u3", "2026-11-06T10:30:00+00:00", 700))
        verdict = await rule.admit("assistant", _msg("u2"))
        assert verdict is not None and verdict.text.startswith("This agent's budget for today is spent (2000 of 2000 tokens)")
        assert telemetry.events[-1].rule == "tokens_per_day"
        # Dollars read the same way, shown to the cent.
        money = BudgetAdmission(store, lambda _a: Budget(usd_per_day=0.1), now=lambda: NOON)
        verdict = await money.admit("assistant", _msg("u1"))
        assert verdict is not None and "(0.13 of 0.10 USD)" in verdict.text
        # A message with no author is held to the agent's budget alone.
        assert await BudgetAdmission(store, lambda _a: Budget(tokens_per_day_per_person=1), now=lambda: NOON).admit("assistant", _msg("")) is None
    finally:
        await store.close()


async def test_the_applications_rule_is_asked_after_the_budget_admits(tmp_path: Path) -> None:
    store = SqliteSessionStore(tmp_path / "db.sqlite")

    class _Own:
        def __init__(self) -> None:
            self.asked: list[str] = []

        async def admit(self, agent: str, msg: IncomingMessage) -> NotAdmitted | None:
            self.asked.append(msg.user_id)
            return NotAdmitted("sign in first", link="https://sign.example") if msg.user_id == "stranger" else None

    try:
        own = _Own()
        rule = BudgetAdmission(store, lambda _a: Budget(tokens_per_day=10), then=own, now=lambda: NOON)
        assert await rule.admit("assistant", _msg("u1")) is None
        verdict = await rule.admit("assistant", _msg("stranger"))
        assert verdict is not None and verdict.code == NOTICE_NOT_ADMITTED and verdict.link == "https://sign.example"
        await store.add_usage(_spent("assistant", "u1", "2026-11-06T09:00:00+00:00", 10))
        verdict = await rule.admit("assistant", _msg("stranger"))
        assert verdict is not None and verdict.code == NOTICE_BUDGET_SPENT  # the budget is asked first
        assert own.asked == ["u1", "stranger"]  # and not asked when the budget refuses
    finally:
        await store.close()


def test_a_turn_over_its_token_budget_has_a_notice_of_its_own() -> None:
    code, text = notice_for(AgentOverBudget("the turn spent 1300 tokens against a budget of 1000"))
    assert code == NOTICE_TOKEN_BUDGET and "token budget" in text and "1300" not in text
