"""Scheduled work: what a trigger means, and the loop that fires it.

``triggers`` is pure arithmetic over a fixed clock; the service that uses it
reaches the rest of the engine only through ports, so the scheduler never sees a
gateway, a flow or a runtime.
"""

from crucible.scheduler.triggers import (
    MIN_INTERVAL_S,
    TRIGGER_AT,
    TRIGGER_CRON,
    TRIGGER_EVERY,
    TRIGGER_KINDS,
    Trigger,
    TriggerError,
    advance_past,
    first_run,
    from_iso,
    grace_window,
    jitter_for,
    next_occurrences,
    parse_trigger,
    to_iso,
    utc_now,
)

__all__ = [
    "MIN_INTERVAL_S",
    "TRIGGER_AT",
    "TRIGGER_CRON",
    "TRIGGER_EVERY",
    "TRIGGER_KINDS",
    "Trigger",
    "TriggerError",
    "advance_past",
    "first_run",
    "from_iso",
    "grace_window",
    "jitter_for",
    "next_occurrences",
    "parse_trigger",
    "to_iso",
    "utc_now",
]
