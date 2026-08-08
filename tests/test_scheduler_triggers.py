"""Schedule arithmetic on a fixed clock (crucible/scheduler/triggers.py)."""

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from crucible.scheduler.triggers import (
    MIN_INTERVAL_S,
    TRIGGER_AT,
    TRIGGER_CRON,
    TRIGGER_EVERY,
    TriggerError,
    advance_past,
    first_run,
    grace_window,
    jitter_for,
    next_occurrences,
    parse_duration,
    parse_trigger,
    to_iso,
)

NOW = datetime(2026, 8, 7, 10, 0, tzinfo=timezone.utc)  # a Friday
BELGRADE = "Europe/Belgrade"  # +01:00 winter, +02:00 summer


# -- parsing -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "seconds"),
    [("30s", 30), ("15m", 900), ("2h", 7200), ("1d", 86400),
     ("1h30m", 5400), ("1w", 604800), ("9:00", 0), ("later", 0)],
)
def test_parse_duration(text: str, seconds: int) -> None:
    assert parse_duration(text) == seconds


def test_a_delay_becomes_a_one_shot_at_an_absolute_moment() -> None:
    trigger = parse_trigger("in 2h", now=NOW)

    assert trigger.kind == TRIGGER_AT
    assert trigger.anchor_at == NOW + timedelta(hours=2)
    assert first_run(trigger, now=NOW) == NOW + timedelta(hours=2)
    assert advance_past(trigger, after=trigger.anchor_at, until=NOW) == (None, 0)


def test_a_bare_wall_time_is_read_in_the_tasks_zone() -> None:
    trigger = parse_trigger("2026-08-09T09:00", now=NOW, tz=BELGRADE)

    # 09:00 in Belgrade (+02:00 in August) is 07:00 UTC — not 09:00 UTC.
    assert to_iso(trigger.anchor_at) == "2026-08-09T07:00:00+00:00"


def test_an_offset_in_the_text_wins_over_the_zone() -> None:
    trigger = parse_trigger("2026-08-09T09:00+00:00", now=NOW, tz=BELGRADE)
    assert to_iso(trigger.anchor_at) == "2026-08-09T09:00:00+00:00"


def test_an_interval_is_parsed_and_floored() -> None:
    trigger = parse_trigger("every 15m", now=NOW)
    assert (trigger.kind, trigger.interval_s) == (TRIGGER_EVERY, 900)


def test_cron_is_accepted_bare_or_prefixed() -> None:
    for spec in ("0 9 * * 1-5", "cron: 0 9 * * 1-5"):
        trigger = parse_trigger(spec, now=NOW, tz=BELGRADE)
        assert (trigger.kind, trigger.cron_expr) == (TRIGGER_CRON, "0 9 * * 1-5")


@pytest.mark.parametrize(
    ("spec", "match"),
    [
        ("every 5s", "at least 60s"),
        ("every soon", "could not read an interval"),
        ("cron: 0 9 * *", "not a valid 5-field cron"),
        ("2020-01-01T00:00+00:00", "has already passed"),
        ("whenever", "could not read"),
        ("", "a schedule is required"),
    ],
)
def test_a_bad_schedule_says_what_is_wrong(spec: str, match: str) -> None:
    with pytest.raises(TriggerError, match=match):
        parse_trigger(spec, now=NOW)


def test_an_unknown_zone_is_refused_before_anything_is_computed() -> None:
    with pytest.raises(TriggerError, match="unknown timezone"):
        parse_trigger("every 15m", now=NOW, tz="Mars/Olympus")


# -- advancing -----------------------------------------------------------------


def test_an_interval_keeps_its_phase_and_counts_what_was_missed() -> None:
    trigger = parse_trigger("every 15m", now=NOW)
    fired = NOW + timedelta(minutes=15)

    # Nothing missed: the tick runs a few seconds after the occurrence.
    assert advance_past(trigger, after=fired, until=fired + timedelta(seconds=3)) == (
        NOW + timedelta(minutes=30), 0,
    )
    # An hour of downtime: four occurrences fell in the gap, and the phase is
    # still on the anchor's :00/:15/:30/:45 — not on the moment we came back.
    nxt, skipped = advance_past(
        trigger, after=fired, until=fired + timedelta(minutes=61, seconds=7)
    )
    assert (nxt, skipped) == (NOW + timedelta(minutes=90), 4)


def test_a_daily_cron_after_a_day_of_downtime_does_not_skip_today() -> None:
    # The OpenClaw bug this design exists to make impossible: computing the next
    # run from the restart time instead of the scheduled one loses a whole day.
    trigger = parse_trigger("0 9 * * *", now=NOW, tz=BELGRADE)
    monday_09 = datetime(2026, 8, 10, 7, 0, tzinfo=timezone.utc)  # 09:00 Belgrade
    came_back = datetime(2026, 8, 10, 8, 30, tzinfo=timezone.utc)  # 10:30 local, late

    nxt, skipped = advance_past(trigger, after=monday_09, until=came_back)

    assert skipped == 0  # today's 09:00 is the occurrence being handled, not a miss
    assert to_iso(nxt) == "2026-08-11T07:00:00+00:00"  # tomorrow, not the day after


def test_cron_counts_every_occurrence_a_long_outage_swallowed() -> None:
    trigger = parse_trigger("0 9 * * *", now=NOW, tz=BELGRADE)
    monday_09 = datetime(2026, 8, 10, 7, 0, tzinfo=timezone.utc)
    thursday_10 = datetime(2026, 8, 13, 8, 0, tzinfo=timezone.utc)

    nxt, skipped = advance_past(trigger, after=monday_09, until=thursday_10)

    assert skipped == 3  # Tue, Wed, Thu
    assert to_iso(nxt) == "2026-08-14T07:00:00+00:00"


def test_a_weekday_cron_skips_the_weekend() -> None:
    trigger = parse_trigger("0 9 * * 1-5", now=NOW, tz=BELGRADE)
    friday_09 = datetime(2026, 8, 7, 7, 0, tzinfo=timezone.utc)

    nxt, _ = advance_past(trigger, after=friday_09, until=friday_09)

    assert nxt is not None and nxt.astimezone(ZoneInfo(BELGRADE)).strftime("%a %H:%M") == "Mon 09:00"


# -- daylight saving -----------------------------------------------------------


def test_a_daily_cron_keeps_its_wall_clock_across_the_spring_change() -> None:
    # Belgrade goes +01:00 -> +02:00 on 2026-03-29. 09:00 local must stay 09:00
    # local, which means the UTC instant moves by an hour.
    trigger = parse_trigger("0 9 * * *", now=NOW, tz=BELGRADE)
    before = datetime(2026, 3, 28, 8, 0, tzinfo=timezone.utc)  # 09:00 +01:00

    nxt, _ = advance_past(trigger, after=before, until=before)

    assert to_iso(nxt) == "2026-03-29T07:00:00+00:00"  # 09:00 +02:00
    assert nxt is not None
    assert nxt.astimezone(ZoneInfo(BELGRADE)).hour == 9


def test_a_daily_cron_keeps_its_wall_clock_across_the_autumn_change() -> None:
    trigger = parse_trigger("0 9 * * *", now=NOW, tz=BELGRADE)
    before = datetime(2026, 10, 24, 7, 0, tzinfo=timezone.utc)  # 09:00 +02:00

    nxt, _ = advance_past(trigger, after=before, until=before)

    assert to_iso(nxt) == "2026-10-25T08:00:00+00:00"  # 09:00 +01:00
    assert nxt is not None
    assert nxt.astimezone(ZoneInfo(BELGRADE)).hour == 9


def test_a_wall_time_that_does_not_exist_runs_at_the_end_of_the_gap() -> None:
    # 02:30 never happens on the spring-forward day; the first instant at or
    # after it is 03:00 local. It must fire, not vanish.
    trigger = parse_trigger("30 2 * * *", now=NOW, tz=BELGRADE)
    before = datetime(2026, 3, 28, 1, 30, tzinfo=timezone.utc)  # 02:30 +01:00

    nxt, _ = advance_past(trigger, after=before, until=before)

    assert nxt is not None
    local = nxt.astimezone(ZoneInfo(BELGRADE))
    assert (local.year, local.month, local.day) == (2026, 3, 29)
    assert (local.hour, local.minute) == (3, 0)


def test_a_wall_time_that_happens_twice_runs_once_on_the_first() -> None:
    trigger = parse_trigger("30 2 * * *", now=NOW, tz=BELGRADE)
    before = datetime(2026, 10, 24, 0, 30, tzinfo=timezone.utc)  # 02:30 +02:00

    nxt, skipped = advance_past(trigger, after=before, until=before)

    assert to_iso(nxt) == "2026-10-25T00:30:00+00:00"  # the +02:00 reading, i.e. the first
    assert skipped == 0


def test_an_interval_is_an_absolute_duration_across_a_dst_change() -> None:
    # `every 24h` is 24 hours. The local wall clock shifting is expected.
    trigger = parse_trigger("every 24h", now=datetime(2026, 3, 28, 8, 0, tzinfo=timezone.utc),
                            tz=BELGRADE)
    fired = trigger.anchor_at

    nxt, _ = advance_past(trigger, after=fired, until=fired)

    assert nxt is not None
    assert nxt == fired + timedelta(hours=24)
    assert nxt.astimezone(ZoneInfo(BELGRADE)).hour == 10  # was 09:00 local


# -- grace, jitter, previews ---------------------------------------------------


def test_the_grace_window_is_half_a_period_within_bounds() -> None:
    assert grace_window(parse_trigger("every 1h", now=NOW), now=NOW) == timedelta(minutes=30)
    # A 2-minute poll: half a period is below the floor.
    assert grace_window(parse_trigger("every 2m", now=NOW), now=NOW) == timedelta(minutes=2)
    # A weekly task: half a period is way over the ceiling.
    assert grace_window(parse_trigger("every 7d", now=NOW), now=NOW) == timedelta(hours=2)
    # A one-shot has no period at all.
    assert grace_window(parse_trigger("in 3h", now=NOW), now=NOW) == timedelta(minutes=15)


def test_the_grace_window_of_a_daily_cron_is_the_ceiling() -> None:
    trigger = parse_trigger("0 9 * * *", now=NOW, tz=BELGRADE)
    assert grace_window(trigger, now=NOW) == timedelta(hours=2)


def test_jitter_is_stable_per_task_and_bounded() -> None:
    first = jitter_for("tsk_abc", period_s=3600, cap_s=30)
    assert first == jitter_for("tsk_abc", period_s=3600, cap_s=30)  # never moves
    assert 0 <= first <= 30
    assert jitter_for("tsk_abc", period_s=0, cap_s=30) == 0  # one-shots are exact
    # A short period gets a proportionally smaller smear.
    assert jitter_for("tsk_abc", period_s=120, cap_s=30) <= 6


def test_jitter_differs_between_tasks_sharing_a_schedule() -> None:
    smears = {jitter_for(f"tsk_{i}", period_s=86400, cap_s=30) for i in range(20)}
    assert len(smears) > 5  # not all on the same second


def test_the_next_few_occurrences_are_previewable() -> None:
    upcoming = next_occurrences(parse_trigger("0 9 * * 1-5", now=NOW, tz=BELGRADE), now=NOW)
    days = [m.astimezone(ZoneInfo(BELGRADE)).strftime("%a %H:%M") for m in upcoming]
    assert days == ["Mon 09:00", "Tue 09:00", "Wed 09:00"]


def test_a_one_shot_previews_exactly_once() -> None:
    trigger = parse_trigger("in 90m", now=NOW)
    assert next_occurrences(trigger, now=NOW) == [NOW + timedelta(minutes=90)]


def test_the_minimum_interval_is_enforced_at_the_boundary() -> None:
    assert parse_trigger(f"every {MIN_INTERVAL_S}s", now=NOW).interval_s == MIN_INTERVAL_S
    with pytest.raises(TriggerError):
        parse_trigger(f"every {MIN_INTERVAL_S - 1}s", now=NOW)
