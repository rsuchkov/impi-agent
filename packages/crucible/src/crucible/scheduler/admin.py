"""Creating, listing and retiring tasks — the write side an agent or a CLI uses.

Separate from the ticker on purpose: this validates and writes rows, the ticker
reads and fires them, and the only thing they share is the store. It resolves the
conversation the same way the widget service does, so a task created mid-turn
belongs to the conversation it was asked for in.
"""

import logging
import secrets
from collections.abc import Callable
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from crucible.ports.tasks import TaskError, TaskView
from crucible.scheduler.triggers import (
    JITTER_CAP_S,
    TRIGGER_EVERY,
    Trigger,
    TriggerError,
    first_run,
    from_iso,
    jitter_for,
    next_occurrences,
    parse_trigger,
    to_iso,
    utc_now,
)
from crucible.store.base import (
    MODE_TURN,
    MODE_WATCH,
    NOTIFY_FAILURES,
    NOTIFY_MODES,
    ON_MISSED_RUN,
    ON_MISSED_SKIP,
    SCHEDULED_MODES,
    STATE_IDLE,
    STATE_PAUSED,
    SessionStore,
    TaskRecord,
    TaskStore,
)

logger = logging.getLogger(__name__)

_MAX_NAME = 40
_MAX_PROMPT = 4000
# A watch's check is the application's hook, not a model run, so it may be
# asked far more often than a task may fire — but not in a tight loop.
MIN_WATCH_S = 15
# What a watch's turn says when the application opened it without a prompt
# of its own: the event block carries what happened.
WATCH_PROMPT = "What this conversation was waiting for has happened: see the event, and act on it."


class TaskAdmin:
    """The TaskService port, over the task store."""

    def __init__(
        self,
        store: TaskStore,
        sessions: SessionStore,
        *,
        default_timezone: str = "UTC",
        max_per_agent: int = 50,
        jitter_cap_s: int = JITTER_CAP_S,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._store = store
        self._sessions = sessions
        self._default_timezone = default_timezone
        self._max_per_agent = max_per_agent
        self._jitter_cap_s = jitter_cap_s
        self._now = clock

    async def schedule(
        self, agent: str, runtime_session_id: str, *, name: str, prompt: str,
        schedule: str, mode: str = MODE_TURN, timezone: str = "",
        notify: str = "failures", on_missed: str = ON_MISSED_RUN,
    ) -> TaskView:
        """From inside a turn: the conversation is the one the turn runs in."""
        record = await self._sessions.get_by_runtime_session(runtime_session_id)
        if record is None:
            raise TaskError("this conversation could not be resolved — nothing was scheduled")
        return await self.schedule_in(
            agent, channel_id=record.channel_id, conversation_id=record.conversation_id,
            kind=record.kind, name=name, prompt=prompt, schedule=schedule, mode=mode,
            timezone=timezone, notify=notify, on_missed=on_missed,
            created_by=record.last_user_id,
        )

    async def schedule_in(
        self, agent: str, *, channel_id: str, conversation_id: str, kind: str,
        name: str, prompt: str, schedule: str, mode: str = MODE_TURN,
        timezone: str = "", notify: str = "failures", on_missed: str = ON_MISSED_RUN,
        created_by: str = "",
    ) -> TaskView:
        """With the conversation named outright — how the CLI schedules, since
        it runs in its own process with no turn to belong to."""
        name = _clean_name(name)
        prompt = prompt.strip()
        if not prompt:
            raise TaskError("a task needs a prompt: what should the agent do?")
        if len(prompt) > _MAX_PROMPT:
            raise TaskError(f"the prompt is too long ({len(prompt)} > {_MAX_PROMPT})")
        if mode not in SCHEDULED_MODES:
            raise TaskError(f"mode must be one of {', '.join(SCHEDULED_MODES)}")
        if notify not in NOTIFY_MODES:
            raise TaskError(f"notify must be one of {', '.join(NOTIFY_MODES)}")
        if on_missed not in (ON_MISSED_RUN, ON_MISSED_SKIP):
            raise TaskError(f"on_missed must be {ON_MISSED_RUN} or {ON_MISSED_SKIP}")
        if await self._store.find_task(agent, name) is not None:
            raise TaskError(f"you already have a task called {name!r}")
        existing = await self._store.list_tasks(agent)
        if len(existing) >= self._max_per_agent:
            raise TaskError(
                f"you already have {len(existing)} tasks (the limit is {self._max_per_agent})"
            )

        now = self._now()
        zone = timezone or self._default_timezone
        try:
            trigger = parse_trigger(schedule, now=now, tz=zone)
        except TriggerError as exc:
            raise TaskError(str(exc)) from exc
        upcoming = first_run(trigger, now=now)
        if upcoming is None:
            raise TaskError(f"{schedule!r} has no next occurrence")

        task_id = f"tsk_{secrets.token_hex(4)}"
        jitter = jitter_for(
            task_id,
            period_s=trigger.interval_s if trigger.recurring else 0,
            cap_s=self._jitter_cap_s,
        )
        stamp = to_iso(now) or ""
        task = TaskRecord(
            id=task_id, agent=agent, name=name, channel_id=channel_id,
            conversation_id=conversation_id, kind=kind, mode=mode,
            prompt=prompt, trigger_kind=trigger.kind, trigger_spec=trigger.spec,
            interval_s=trigger.interval_s, cron_expr=trigger.cron_expr,
            timezone=trigger.timezone, anchor_at=to_iso(trigger.anchor_at) or stamp,
            # due_at carries the smear, next_run_at stays the honest time — the
            # first occurrence included, or a fleet of daily tasks created in one
            # sitting would all fire on the same second the first time round.
            next_run_at=to_iso(upcoming), due_at=to_iso(_shift(upcoming, jitter)),
            jitter_s=jitter,
            state=STATE_IDLE, claim_owner="", claim_at="", lease_until="",
            on_missed=on_missed, notify=notify, deadline_s=0,
            created_by=created_by, created_by_username="",
            created_at=stamp, updated_at=stamp, last_run_at="", last_status="",
            run_count=0, miss_count=0, consecutive_failures=0,
        )
        await self._store.create_task(task)
        logger.info(
            "task %s scheduled for %s in %s: %s", task_id, agent, conversation_id,
            trigger.spec,
        )
        return _view(task, upcoming=next_occurrences(trigger, now=now, count=3))

    async def watch(
        self, agent: str, runtime_session_id: str, *, name: str, watch: str,
        every_s: int, until: datetime, prompt: str = "",
    ) -> TaskView:
        record = await self._sessions.get_by_runtime_session(runtime_session_id)
        if record is None:
            raise TaskError("this conversation could not be resolved — nothing is watched")
        return await self.watch_in(
            agent, channel_id=record.channel_id, conversation_id=record.conversation_id,
            kind=record.kind, name=name, watch=watch, every_s=every_s, until=until,
            prompt=prompt, created_by=record.last_user_id,
        )

    async def watch_in(
        self, agent: str, *, channel_id: str, conversation_id: str, kind: str,
        name: str, watch: str, every_s: int, until: datetime, prompt: str = "",
        created_by: str = "",
    ) -> TaskView:
        """A watch task: the scheduler asks the application's hook about
        ``watch`` every ``every_s`` seconds until ``until``, and the agent gets
        one turn here when the answer is yes — or when the wait is over."""
        name = _clean_name(name)
        watch = watch.strip()
        if not watch:
            raise TaskError("a watch needs a key: what should the application be asked about?")
        if every_s < MIN_WATCH_S:
            raise TaskError(f"a watch is checked at most every {MIN_WATCH_S}s")
        now = self._now()
        if until.tzinfo is None:
            raise TaskError("`until` must be timezone-aware")
        if until <= now:
            raise TaskError("`until` is already past")
        prompt = prompt.strip() or WATCH_PROMPT
        if len(prompt) > _MAX_PROMPT:
            raise TaskError(f"the prompt is too long ({len(prompt)} > {_MAX_PROMPT})")
        if await self._store.find_task(agent, name) is not None:
            raise TaskError(f"you already have a task called {name!r}")
        existing = await self._store.list_tasks(agent)
        if len(existing) >= self._max_per_agent:
            raise TaskError(
                f"you already have {len(existing)} tasks (the limit is {self._max_per_agent})"
            )
        trigger = Trigger(
            kind=TRIGGER_EVERY, spec=f"every {every_s}s", anchor_at=now,
            interval_s=every_s, timezone=self._default_timezone,
        )
        first = now + timedelta(seconds=every_s)
        stamp = to_iso(now) or ""
        task_id = f"tsk_{secrets.token_hex(4)}"
        task = TaskRecord(
            id=task_id, agent=agent, name=name, channel_id=channel_id,
            conversation_id=conversation_id, kind=kind, mode=MODE_WATCH,
            prompt=prompt, trigger_kind=trigger.kind, trigger_spec=trigger.spec,
            interval_s=trigger.interval_s, cron_expr="", timezone=trigger.timezone,
            anchor_at=stamp, next_run_at=to_iso(first), due_at=to_iso(first), jitter_s=0,
            state=STATE_IDLE, claim_owner="", claim_at="", lease_until="",
            on_missed=ON_MISSED_RUN, notify=NOTIFY_FAILURES, deadline_s=0,
            created_by=created_by, created_by_username="",
            created_at=stamp, updated_at=stamp, last_run_at="", last_status="",
            run_count=0, miss_count=0, consecutive_failures=0,
            watch=watch, expires_at=to_iso(until) or "",
        )
        await self._store.create_task(task)
        logger.info(
            "task %s watches %s for %s in %s, every %ds until %s",
            task_id, watch, agent, conversation_id, every_s, task.expires_at,
        )
        return _view(task)

    async def list_tasks(self, agent: str) -> list[TaskView]:
        return [_view(task) for task in await self._store.list_tasks(agent)]

    async def cancel(self, agent: str, task: str) -> TaskView:
        found = await self._find(agent, task)
        await self._store.delete_task(found.id)
        logger.info("task %s cancelled", found.id)
        return _view(found)

    async def set_paused(self, agent: str, task: str, paused: bool) -> TaskView:
        found = await self._find(agent, task)
        now = self._now()
        upcoming = None if paused else first_run(_trigger_of(found), now=now)
        ok = await self._store.set_paused(
            found.id, paused, now=to_iso(now) or "",
            next_run_at=to_iso(upcoming),
            due_at=to_iso(_shift(upcoming, found.jitter_s)),
        )
        if not ok:
            raise TaskError(f"{found.name!r} is running right now — try again in a moment")
        refreshed = await self._store.get_task(found.id)
        return _view(refreshed or found)

    async def _find(self, agent: str, task: str) -> TaskRecord:
        """By id or by name, and only among this agent's own — one agent must
        never be able to touch another's schedule."""
        found = await self._store.get_task(task) or await self._store.find_task(agent, task)
        if found is None or found.agent != agent:
            raise TaskError(f"no task {task!r}")
        return found


# -- rendering ------------------------------------------------------------------


def _clean_name(name: str) -> str:
    cleaned = " ".join(name.split())[:_MAX_NAME].strip()
    if not cleaned:
        raise TaskError("a task needs a short name")
    return cleaned


def _trigger_of(task: TaskRecord) -> Trigger:
    return Trigger(
        kind=task.trigger_kind, spec=task.trigger_spec,
        anchor_at=from_iso(task.anchor_at) or utc_now(),
        interval_s=task.interval_s, cron_expr=task.cron_expr, timezone=task.timezone,
    )


def _shift(moment: datetime | None, seconds: int) -> datetime | None:
    return moment + timedelta(seconds=seconds) if moment is not None else None


def local_time(moment: datetime | None, timezone: str) -> str:
    """A moment as the person who scheduled it would read it."""
    if moment is None:
        return ""
    zone = ZoneInfo(timezone) if timezone else ZoneInfo("UTC")
    return f"{moment.astimezone(zone):%Y-%m-%d %H:%M} ({timezone or 'UTC'})"


def _view(task: TaskRecord, *, upcoming: list[datetime] | None = None) -> TaskView:
    return TaskView(
        id=task.id, name=task.name, prompt=task.prompt, schedule=task.trigger_spec,
        mode=task.mode, state=task.state, timezone=task.timezone or "UTC",
        next_run=local_time(from_iso(task.next_run_at), task.timezone),
        last_run=local_time(from_iso(task.last_run_at), task.timezone),
        last_status=task.last_status, notify=task.notify,
        upcoming=tuple(local_time(m, task.timezone) for m in (upcoming or ())),
        watch=task.watch, until=local_time(from_iso(task.expires_at or None), task.timezone),
    )


__all__ = ["TaskAdmin", "TaskError", "TaskView", "local_time", "STATE_PAUSED"]
