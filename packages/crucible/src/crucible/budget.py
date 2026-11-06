"""An agent's budget at the model, held before a turn.

``AgentSpec.budget`` says what an agent may spend in a calendar day (UTC) —
tokens or the runtime's own dollar estimate, for the agent as a whole and for
each person it answers. The ledger (``UsageStore``) has what every turn cost;
this rule reads the day's sum before a turn and, past a limit, does not admit
the turn (``ports.admission``): nothing runs, and the person is told their
own budget with the agent, or the agent's, is spent until the day turns. The
turn's own budget (``tokens_per_turn``) is the runtime's to hold, while the
turn runs. An application with a rule of its own chains it behind
(``then``): the budget is asked first, and a turn it does not admit is asked
of nobody else.
"""

import logging
from collections.abc import Callable
from datetime import datetime, timezone

from crucible.ports.admission import NotAdmitted, TurnAdmission
from crucible.ports.agent.spec import Budget
from crucible.ports.chat.types import IncomingMessage
from crucible.ports.telemetry import BudgetHit, Telemetry
from crucible.store.base import UsageStore

logger = logging.getLogger(__name__)

NOTICE_BUDGET_SPENT = "budget_spent"
AGENT_SPENT = "This agent's budget for today is spent ({spent} of {limit} {unit}); it answers again after 00:00 UTC."
PERSON_SPENT = "Your budget with this agent for today is spent ({spent} of {limit} {unit}); ask again after 00:00 UTC."


def day_start(now: datetime) -> str:
    """The calendar day's first moment, UTC, as the ledger keys time."""
    return now.astimezone(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).isoformat(timespec="seconds")


class BudgetAdmission:
    """``TurnAdmission`` by the agent's budget: the day's spending, read from
    the ledger, against ``budget_for(agent)``; ``then`` is asked when the
    budget admits."""

    def __init__(
        self,
        ledger: UsageStore,
        budget_for: Callable[[str], Budget],
        *,
        then: TurnAdmission | None = None,
        telemetry: Telemetry | None = None,
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self._ledger = ledger
        self._budget_for = budget_for
        self._then = then
        self._telemetry = telemetry
        self._now = now

    async def admit(self, agent: str, msg: IncomingMessage) -> NotAdmitted | None:
        budget = self._budget_for(agent)
        if budget.daily:
            since = day_start(self._now())
            if budget.tokens_per_day or budget.usd_per_day:
                spent = await self._ledger.usage_since(agent, since)
                hit = self._over(agent, msg.user_id, "tokens_per_day", spent.total, budget.tokens_per_day, AGENT_SPENT, "tokens") \
                    or self._over(agent, msg.user_id, "usd_per_day", spent.cost_usd, budget.usd_per_day, AGENT_SPENT, "USD")
                if hit is not None:
                    return hit
            if msg.user_id and (budget.tokens_per_day_per_person or budget.usd_per_day_per_person):
                spent = await self._ledger.usage_since(agent, since, user_id=msg.user_id)
                hit = self._over(
                    agent, msg.user_id, "tokens_per_day_per_person", spent.total, budget.tokens_per_day_per_person,
                    PERSON_SPENT, "tokens",
                ) or self._over(
                    agent, msg.user_id, "usd_per_day_per_person", spent.cost_usd, budget.usd_per_day_per_person,
                    PERSON_SPENT, "USD",
                )
                if hit is not None:
                    return hit
        return await self._then.admit(agent, msg) if self._then is not None else None

    def _over(
        self, agent: str, user_id: str, rule: str, spent: float, limit: float, words: str, unit: str,
    ) -> NotAdmitted | None:
        if not limit or spent < limit:
            return None
        logger.info("agent %s: %s spent (%s of %s %s) — a turn for %s is not admitted", agent, rule, spent, limit, unit, user_id or "nobody")
        if self._telemetry is not None:
            try:
                self._telemetry.record(BudgetHit(agent=agent, user_id=user_id, rule=rule, spent=spent, limit=limit))
            except Exception:  # noqa: BLE001 - telemetry must never cost the turn
                logger.warning("telemetry failed to record a budget hit", exc_info=True)
        shown_spent = f"{spent:.2f}" if unit == "USD" else f"{int(spent)}"
        shown_limit = f"{limit:.2f}" if unit == "USD" else f"{int(limit)}"
        return NotAdmitted(text=words.format(spent=shown_spent, limit=shown_limit, unit=unit), code=NOTICE_BUDGET_SPENT)
