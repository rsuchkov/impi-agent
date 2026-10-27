"""The engine's state, told on a timer (``ports.telemetry.EngineState``).

The other events say what happened; this one says how things stand — how many
sessions are alive and busy, how many turns wait — for whoever reads a gauge.
A loop, like the heartbeat: the application supervises it.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable

from crucible.ports.agent.runtime import RuntimeControl
from crucible.ports.telemetry import EngineState, Telemetry

logger = logging.getLogger(__name__)


async def tell_engine_state(
    telemetry: Telemetry,
    *,
    control: RuntimeControl | None,
    agents: Callable[[], int],
    http_turns_active: Callable[[], int] = lambda: 0,
    interval_s: float,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> None:
    """Tell ``EngineState`` every ``interval_s`` seconds, until cancelled.
    ``interval_s`` of 0 tells nothing and returns at once."""
    if interval_s <= 0:
        return
    while True:
        try:
            telemetry.record(engine_state(control=control, agents=agents(), http_turns_active=http_turns_active()))
        except Exception:  # noqa: BLE001 - telemetry must never stop the engine
            logger.warning("telemetry failed to record the engine's state", exc_info=True)
        await sleep(interval_s)


def engine_state(*, control: RuntimeControl | None, agents: int, http_turns_active: int) -> EngineState:
    """One reading, from what the runtime says of itself."""
    stats = control.stats() if control is not None else None
    return EngineState(
        agents=agents,
        sessions_alive=stats.alive if stats else 0,
        sessions_busy=stats.busy if stats else 0,
        capacity=stats.capacity if stats else 0,
        turns_waiting=stats.waiting if stats else 0,
        http_turns_active=http_turns_active,
    )
