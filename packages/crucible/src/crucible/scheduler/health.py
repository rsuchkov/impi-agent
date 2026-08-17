"""Is the scheduler alive, and when does it next wake?

One verdict function, so a health check and a task listing can never disagree.
The failure it exists to expose: a timer that quietly stopped looks exactly like
a timer with nothing to do. The heartbeat is written at the END of a tick, so a
fresh timestamp proves a tick completed rather than merely started.
"""

from datetime import datetime

from crucible.scheduler.triggers import from_iso
from crucible.store.base import SchedulerHeartbeat

ALIVE = "alive"
STALE = "stale"  # ticked once, but not lately — the loop is wedged or gone
ABSENT = "absent"  # turned off on purpose
NEVER = "never"  # enabled, but no tick has ever been recorded

# How many missed ticks before we call it stale. Three, so one slow tick (a
# sluggish disk, a burst of due work) is not an alarm.
_STALE_TICKS = 3


def liveness(
    beat: SchedulerHeartbeat | None, *, now: datetime, enabled: bool = True
) -> tuple[str, str]:
    """``(verdict, one line for a human)``."""
    if not enabled:
        return ABSENT, "the scheduler is turned off (SCHEDULER_ENABLED=false)"
    if beat is None:
        return NEVER, "no scheduler has ever ticked against this database"

    ticked = from_iso(beat.last_tick_at)
    if ticked is None:
        return NEVER, f"unreadable last tick: {beat.last_tick_at!r}"
    age = (now - ticked).total_seconds()

    if age > max(beat.interval_s, 1.0) * _STALE_TICKS:
        return STALE, (
            f"no tick for {age:.0f}s (expected every {beat.interval_s:.0f}s) — "
            f"pid {beat.pid}, up since {_stamp(beat.started_at)}, "
            f"last tick {_stamp(beat.last_tick_at)}"
        )

    detail = f"tick #{beat.tick_seq} {age:.0f}s ago, {beat.running_count} run(s) in flight"
    if beat.next_wake_at:
        detail += (
            f"; next {beat.next_task_name or beat.next_task_id}"
            f" at {_stamp(beat.next_wake_at)}"
        )
    else:
        detail += "; nothing scheduled"
    if beat.last_error:
        detail += f"; last error {_stamp(beat.last_error_at)}: {beat.last_error}"
    return ALIVE, detail


def _stamp(iso: str) -> str:
    """Heartbeat times are engine state, always UTC — read them the way the rest
    of the surfaces print a moment, not as raw ISO."""
    moment = from_iso(iso)
    return f"{moment:%Y-%m-%d %H:%M} (UTC)" if moment else iso


def another_scheduler(
    first: SchedulerHeartbeat, second: SchedulerHeartbeat
) -> bool:
    """Whether two readings show a second engine scheduling against this
    database. The claim protocol makes that SAFE; this makes it visible."""
    return first.scheduler_id != second.scheduler_id
