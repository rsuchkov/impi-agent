"""Schedule arithmetic: what a trigger means and when it fires next.

Pure functions over a fixed clock — no I/O, no store, no runtime — because this
is where scheduling goes wrong. Two rules carry most of the weight:

- **The next occurrence is computed from the previous SCHEDULED instant**, never
  from "now". A restart therefore cannot move a schedule's phase, which is what
  makes "the engine was down at 09:00, so today is skipped" unreachable.
- **One function decides when a trigger fires.** Two places computing the same
  fire time is how a task ends up running twice, an hour apart, on the day the
  clocks change.

Cron expressions are stepped in the task's own IANA zone as *wall clock*, then
resolved to an instant; intervals are absolute durations in UTC (``every 24h``
is 24 hours, not "the same local time tomorrow").
"""

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from croniter import croniter

TRIGGER_AT = "at"  # one-shot, at an absolute moment
TRIGGER_EVERY = "every"  # a fixed interval from an anchor
TRIGGER_CRON = "cron"  # a 5-field cron expression in the task's zone
TRIGGER_KINDS = (TRIGGER_AT, TRIGGER_EVERY, TRIGGER_CRON)

# The floor on how often a task may run. Synthetic turns bypass the LoopGuard, so
# this is the only thing standing between a typo and a runaway agent.
MIN_INTERVAL_S = 60
# How late a run may be and still be worth doing: half the period, within these
# bounds. A five-minute poll that is an hour late is pointless; a daily digest
# two hours late is still the digest.
MIN_GRACE_S = 120
MAX_GRACE_S = 7200
ONESHOT_GRACE_S = 900
# A cron so far behind that enumerating what was missed is pointless.
MAX_CRON_STEPS = 10_000

_DURATION = re.compile(r"(\d+)\s*([smhdw])", re.IGNORECASE)
_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}
_EVERY_PREFIX = re.compile(r"^(every|each)\s+", re.IGNORECASE)
_AT_PREFIX = re.compile(r"^(at|in|after)\s+", re.IGNORECASE)
_CRON_PREFIX = re.compile(r"^cron:?\s+", re.IGNORECASE)


class TriggerError(ValueError):
    """A schedule that cannot be understood, or that asks for the impossible."""


@dataclass(frozen=True)
class Trigger:
    """A parsed schedule. ``spec`` is what the human wrote, kept for display;
    everything else is resolved, so nothing re-parses text at fire time.

    ``anchor_at`` is the phase an interval counts from — never re-anchored, so a
    restart (or a pause and resume) reproduces the original rhythm."""

    kind: str
    spec: str
    anchor_at: datetime
    interval_s: int = 0
    cron_expr: str = ""
    timezone: str = ""  # IANA name; "" means UTC

    @property
    def zone(self) -> ZoneInfo:
        return _zone(self.timezone)

    @property
    def recurring(self) -> bool:
        return self.kind != TRIGGER_AT


# -- time helpers -------------------------------------------------------------


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def to_iso(moment: datetime | None) -> str | None:
    """The store's timestamp format: UTC, seconds precision, so string order is
    time order. None stays None — the store uses NULL for "no next run"."""
    if moment is None:
        return None
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds")


def from_iso(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _zone(name: str) -> ZoneInfo:
    if not name:
        return ZoneInfo("UTC")
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise TriggerError(f"unknown timezone {name!r}") from exc


def _resolve_local(wall: datetime, zone: ZoneInfo) -> datetime:
    """A local wall-clock reading as an absolute instant.

    Twice-a-year edge cases, decided here once so nothing else has to:
    a time that happens **twice** (clocks went back) is the first of the two;
    a time that does **not exist** (clocks jumped forward) becomes the first
    instant that is at or after it — i.e. the end of the gap."""
    candidate = wall.replace(tzinfo=zone, fold=0)
    if candidate.astimezone(timezone.utc).astimezone(zone).replace(tzinfo=None) == wall:
        return candidate.astimezone(timezone.utc)
    # Inside a gap. The two folds bracket the transition, and local time is
    # monotonic across it, so the first valid instant is a plain bisection away.
    low = wall.replace(tzinfo=zone, fold=1).astimezone(timezone.utc)
    high = candidate.astimezone(timezone.utc)
    if low > high:
        low, high = high, low
    while high - low > timedelta(seconds=1):
        middle = low + (high - low) / 2
        if middle.astimezone(zone).replace(tzinfo=None) < wall:
            low = middle
        else:
            high = middle
    return high.replace(microsecond=0)


# -- parsing ------------------------------------------------------------------


def parse_duration(text: str) -> int:
    """``90m``, ``2h``, ``1h30m``, ``3d`` -> seconds. 0 when nothing parses."""
    matches = _DURATION.findall(text.strip())
    if not matches:
        return 0
    # The whole string must be duration, or "9:00" would read as 9 seconds.
    if _DURATION.sub("", text.strip()).strip():
        return 0
    return sum(int(value) * _UNIT_SECONDS[unit.lower()] for value, unit in matches)


def parse_trigger(spec: str, *, now: datetime, tz: str = "") -> Trigger:
    """Understand a schedule as written, and resolve it once.

    Accepted: ``in 2h`` / ``30m`` (one-shot), an ISO 8601 moment (bare = the
    task's zone), ``every 15m``, and a 5-field cron expression with or without a
    ``cron:`` prefix."""
    text = " ".join(spec.split())
    if not text:
        raise TriggerError("a schedule is required")
    zone = _zone(tz)  # validates the name before anything else is computed

    if _EVERY_PREFIX.match(text):
        body = _EVERY_PREFIX.sub("", text)
        seconds = parse_duration(body)
        if not seconds:
            raise TriggerError(
                f"could not read an interval in {body!r} — try '15m', '2h', '1d'"
            )
        if seconds < MIN_INTERVAL_S:
            raise TriggerError(
                f"an interval must be at least {MIN_INTERVAL_S}s (asked for {seconds}s)"
            )
        return Trigger(kind=TRIGGER_EVERY, spec=text, anchor_at=_floor(now),
                       interval_s=seconds, timezone=tz)

    cron_body = _CRON_PREFIX.sub("", text) if _CRON_PREFIX.match(text) else text
    if len(cron_body.split()) == 5 and croniter.is_valid(cron_body):
        return Trigger(kind=TRIGGER_CRON, spec=text, anchor_at=_floor(now),
                       cron_expr=cron_body, timezone=tz)
    if _CRON_PREFIX.match(text):  # said "cron:" and meant it — don't guess further
        raise TriggerError(f"{cron_body!r} is not a valid 5-field cron expression")

    moment = _parse_moment(_AT_PREFIX.sub("", text), now=now, zone=zone)
    if moment is None:
        raise TriggerError(
            f"could not read {text!r} as a schedule — expected a delay ('in 2h'), "
            "a moment ('2026-08-09T09:00'), an interval ('every 15m') "
            "or a cron expression ('0 9 * * 1-5')"
        )
    if moment <= now:
        raise TriggerError(f"{to_iso(moment)} has already passed")
    return Trigger(kind=TRIGGER_AT, spec=text, anchor_at=moment, timezone=tz)


def _parse_moment(text: str, *, now: datetime, zone: ZoneInfo) -> datetime | None:
    seconds = parse_duration(text)
    if seconds:
        return _floor(now + timedelta(seconds=seconds))
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo:
        return _floor(parsed.astimezone(timezone.utc))
    # A bare wall time means the task's zone, not the server's.
    return _floor(_resolve_local(parsed, zone))


def _floor(moment: datetime) -> datetime:
    """Seconds precision, matching what the store keeps."""
    return moment.astimezone(timezone.utc).replace(microsecond=0)


# -- when it fires ------------------------------------------------------------


def first_run(trigger: Trigger, *, now: datetime) -> datetime | None:
    """The first occurrence at or after ``now``. None only if there is none."""
    if trigger.kind == TRIGGER_AT:
        return trigger.anchor_at if trigger.anchor_at > now else None
    nxt, _ = advance_past(trigger, after=trigger.anchor_at, until=now)
    return nxt


def advance_past(
    trigger: Trigger, *, after: datetime, until: datetime
) -> tuple[datetime | None, int]:
    """``(first occurrence strictly after ``until``, occurrences in (after, until])``.

    Always anchored to ``after`` — a scheduled instant — or to the trigger's own
    anchor. Never to a wall-clock reading, which is the bug that makes a daily
    task skip a whole day after a restart."""
    if trigger.kind == TRIGGER_AT:
        return None, 0  # a one-shot has no successor
    if trigger.kind == TRIGGER_EVERY:
        return _advance_interval(trigger, after=after, until=until)
    return _advance_cron(trigger, after=after, until=until)


def _advance_interval(
    trigger: Trigger, *, after: datetime, until: datetime
) -> tuple[datetime, int]:
    # Arithmetic, not iteration: an interval that fell behind by a month must not
    # cost a month of loop steps, and the phase stays exactly on the anchor.
    period = timedelta(seconds=trigger.interval_s)
    anchor = trigger.anchor_at
    index_until = _index_of(anchor, period, until)
    index_after = _index_of(anchor, period, after)
    return anchor + (index_until + 1) * period, max(0, index_until - index_after)


def _index_of(anchor: datetime, period: timedelta, moment: datetime) -> int:
    """How many periods have elapsed at ``moment`` (-1 before the anchor)."""
    elapsed = (moment - anchor).total_seconds()
    if elapsed < 0:
        return -1
    return int(elapsed // period.total_seconds())


def _advance_cron(
    trigger: Trigger, *, after: datetime, until: datetime
) -> tuple[datetime | None, int]:
    zone = trigger.zone
    # Step wall clock in the task's own zone, then resolve each reading to an
    # instant — so "09:00" stays 09:00 across a DST change.
    steps = croniter(trigger.cron_expr, after.astimezone(zone).replace(tzinfo=None))
    skipped = 0
    for _ in range(MAX_CRON_STEPS):
        moment = _resolve_local(steps.get_next(datetime), zone)
        if moment > until:
            return moment, skipped
        skipped += 1
    # Absurdly far behind (years of a per-minute schedule). Jump straight to the
    # next occurrence and let the caller report an uncountable backlog.
    ahead = croniter(trigger.cron_expr, until.astimezone(zone).replace(tzinfo=None))
    return _resolve_local(ahead.get_next(datetime), zone), -1


def next_occurrences(trigger: Trigger, *, now: datetime, count: int = 3) -> list[datetime]:
    """The next few fire times — echoed back when a task is created, so a wrong
    cron expression is obvious immediately instead of at 03:00 on Sunday."""
    moments: list[datetime] = []
    cursor = now
    for _ in range(count):
        nxt, _ = advance_past(trigger, after=cursor, until=cursor)
        if nxt is None:
            if trigger.kind == TRIGGER_AT and trigger.anchor_at > now and not moments:
                moments.append(trigger.anchor_at)
            break
        moments.append(nxt)
        cursor = nxt
    return moments


def period_of(trigger: Trigger, *, now: datetime) -> timedelta:
    """How long between two occurrences — exact for an interval, measured for a
    cron expression (which has no single period, so we take the next gap)."""
    if trigger.kind == TRIGGER_EVERY:
        return timedelta(seconds=trigger.interval_s)
    if trigger.kind == TRIGGER_CRON:
        upcoming = next_occurrences(trigger, now=now, count=2)
        if len(upcoming) == 2:
            return upcoming[1] - upcoming[0]
    return timedelta(0)


def grace_window(trigger: Trigger, *, now: datetime) -> timedelta:
    """How late an occurrence may be and still run. Half the period keeps a late
    run meaningful: it fires before the next one is due, never after."""
    if trigger.kind == TRIGGER_AT:
        return timedelta(seconds=ONESHOT_GRACE_S)
    half = period_of(trigger, now=now).total_seconds() / 2
    return timedelta(seconds=min(max(half, MIN_GRACE_S), MAX_GRACE_S))


def jitter_for(task_id: str, *, period_s: int, cap_s: int) -> int:
    """A stable per-task offset, so a dozen tasks written ``0 9 * * *`` do not
    all spawn a subprocess on the same second. Derived from the id, so it never
    changes; zero for a one-shot, where 09:00 has to mean 09:00."""
    if period_s <= 0 or cap_s <= 0:
        return 0
    bound = min(cap_s, max(1, period_s // 20))
    digest = hashlib.sha256(task_id.encode()).digest()
    return int.from_bytes(digest[:4], "big") % (bound + 1)
