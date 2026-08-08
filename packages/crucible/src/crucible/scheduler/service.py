"""The loop that fires due tasks, and the accounting that says what happened.

Shape of a tick: read what is due, decide one of four things for each task
(overlap / too soon after a restart / missed / run), and write a heartbeat. Every
decision leaves a row behind, so "why didn't it run" always has an answer.

Three properties this file exists to hold:

- **Nothing fires twice.** The store's claim advances the schedule in the same
  transaction as the lease, so an occurrence is spoken for before any work
  starts and a crash cannot bring it back.
- **The loop cannot die quietly.** A tick that raises is logged, recorded in the
  heartbeat and followed by another tick; only cancellation ends the loop. The
  engine supervises it beside the gateways.
- **A failure always reaches a human.** Every terminal run is written with
  ``notified = 0`` and delivered later, so the notice survives the restart that
  caused the failure.
"""

import asyncio
import logging
import os
import secrets
import uuid
from collections.abc import Callable
from datetime import datetime, timedelta

from crucible.ports.chat.flow import TurnOutcome
from crucible.scheduler.ports import (
    DispatchError,
    Notifier,
    PromptRunner,
    TurnDispatcher,
    TurnRequest,
)
from crucible.scheduler.triggers import (
    Trigger,
    advance_past,
    from_iso,
    grace_window,
    to_iso,
    utc_now,
)
from crucible.store.base import (
    MODE_PROMPT,
    NOTIFY_ALWAYS,
    NOTIFY_NEVER,
    ON_MISSED_SKIP,
    RUN_CANCELLED,
    RUN_DEADLINE,
    RUN_EMPTY,
    RUN_ERROR,
    RUN_INTERRUPTED,
    RUN_MISSED,
    RUN_NO_AGENT,
    RUN_OK,
    RUN_OVERLAP,
    RUN_RUNNING,
    RUN_TIMEOUT,
    STATE_DONE,
    STATE_IDLE,
    STATE_PAUSED,
    STATE_RUNNING,
    TRIGGERED_CATCHUP,
    TRIGGERED_SCHEDULE,
    SchedulerHeartbeat,
    TaskRecord,
    TaskRunRecord,
    TaskStore,
)

logger = logging.getLogger(__name__)

# TurnOutcome -> what the run is recorded as. The three the flow already told the
# user about keep their own statuses so the notifier can stay quiet about them.
_STATUS_BY_OUTCOME = {
    TurnOutcome.REPLIED: RUN_OK,
    TurnOutcome.ACTED: RUN_OK,
    TurnOutcome.EMPTY: RUN_EMPTY,
    TurnOutcome.TIMEOUT: RUN_TIMEOUT,
    TurnOutcome.ERROR: RUN_ERROR,
    TurnOutcome.DUPLICATE: RUN_ERROR,
}
# In turn mode the flow posts its own message for these, so a notice would be a
# second one about the same thing.
_FLOW_REPORTED = frozenset({RUN_TIMEOUT, RUN_ERROR, RUN_EMPTY})
_SUCCEEDED = frozenset({RUN_OK})


class Scheduler:
    """One ticker over the task store. Started by the composition root and
    awaited beside the gateways, so a crash restarts it instead of ending it."""

    def __init__(
        self,
        store: TaskStore,
        *,
        dispatcher: TurnDispatcher,
        prompts: PromptRunner,
        notifier: Notifier,
        tick_s: float = 20.0,
        startup_grace_s: float = 60.0,
        lease_s: float = 3600.0,
        run_deadline_s: float = 900.0,
        max_concurrent: int = 2,
        max_failures: int = 5,
        keep_runs: int = 50,
        retention_days: int = 30,
        version: str = "",
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._store = store
        self._dispatcher = dispatcher
        self._prompts = prompts
        self._notifier = notifier
        self._tick_s = tick_s
        self._startup_grace_s = startup_grace_s
        self._lease_s = lease_s
        self._run_deadline_s = run_deadline_s
        self._max_concurrent = max_concurrent
        self._max_failures = max_failures
        self._keep_runs = keep_runs
        self._retention_days = retention_days
        self._version = version
        self._now = clock
        # New on every process start: that is what makes a run left behind by a
        # previous engine recognizably not ours.
        self._id = uuid.uuid4().hex[:12]
        self._started_at = self._now()
        self._inflight: dict[str, asyncio.Task[None]] = {}
        self._tick_seq = 0
        self._last_error = ""
        self._last_error_at = ""

    # -- the loop ------------------------------------------------------------

    async def run(self) -> None:
        """Tick until cancelled. Never exits on its own: a failing tick is
        recorded and followed by the next one, because a scheduler that dies
        quietly is indistinguishable from one with nothing to do."""
        await self.recover()
        logger.info(
            "scheduler %s up: tick %.0fs, deadline %.0fs, %d concurrent",
            self._id, self._tick_s, self._run_deadline_s, self._max_concurrent,
        )
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._note_error(exc)
                logger.exception("scheduler tick failed")
                await self._write_heartbeat()
            await asyncio.sleep(self._tick_s)

    async def stop(self) -> None:
        """Cancel whatever is in flight. Best-effort ``cancelled`` rows; the
        guaranteed verdict is the next start's reclaim."""
        for task in list(self._inflight.values()):
            task.cancel()
        if self._inflight:
            await asyncio.gather(*self._inflight.values(), return_exceptions=True)

    async def recover(self) -> None:
        """What a previous engine left mid-run. Deliberately does NOT sweep for
        catch-up: the ordinary tick decides run-vs-miss, and one code path making
        that call is worth more than a faster first reaction."""
        orphans = await self._store.reclaim_orphans(scheduler_id=self._id, now=self._iso())
        for orphan in orphans:
            logger.warning(
                "task %s: run %s was interrupted by a restart", orphan.task_id, orphan.run_id
            )
        await self._store.prune_runs(
            keep_per_task=self._keep_runs,
            before=self._iso(self._now() - timedelta(days=self._retention_days)),
        )

    async def tick(self) -> None:
        now = self._now()
        self._tick_seq += 1
        for task in await self._store.due_tasks(self._iso(now)):
            if len(self._inflight) >= self._max_concurrent:
                # The rest keep their due_at and are picked up next tick; better
                # late than piling subprocesses onto a busy engine.
                break
            await self._dispatch(task, now)
        await self._deliver_notices()
        await self._write_heartbeat()

    # -- deciding what to do with a due task ---------------------------------

    async def _dispatch(self, task: TaskRecord, now: datetime) -> None:
        scheduled = self._scheduled_instant(task)
        if scheduled is None:  # unreadable row; leave it alone rather than loop
            logger.error("task %s has no readable next run", task.id)
            return
        trigger = _trigger_of(task)
        lateness = now - scheduled
        upcoming, skipped = advance_past(trigger, after=scheduled, until=now)
        next_due = _with_jitter(upcoming, task.jitter_s)

        if task.state == STATE_RUNNING:
            # claim_due already advanced the schedule, so a RUNNING task whose
            # due_at has come round again has outlasted its own period.
            await self._skip(
                task, scheduled, upcoming, next_due, now,
                status=RUN_OVERLAP, coalesced=skipped + 1,
                detail=f"the previous run has been going for {_short(lateness)}",
            )
            return

        if lateness.total_seconds() > self._fresh_slack() and self._within_startup_grace(now):
            # Gateways may still be logging in. A punctual occurrence fires
            # normally; only a late one waits, and only for a minute.
            return

        grace = grace_window(trigger, now=now)
        if task.on_missed == ON_MISSED_SKIP or lateness > grace:
            why = (
                "on_missed=skip"
                if task.on_missed == ON_MISSED_SKIP
                else f"late by {_short(lateness)} (window {_short(grace)})"
            )
            await self._skip(
                task, scheduled, upcoming, next_due, now,
                status=RUN_MISSED, coalesced=skipped + 1, detail=why,
            )
            return

        late = lateness.total_seconds() > self._fresh_slack()
        run = await self._store.claim_due(
            task_id=task.id,
            seen_due_at=task.due_at or "",
            next_run_at=to_iso(upcoming),
            due_at=to_iso(next_due),
            run=self._new_run(
                task, scheduled, RUN_RUNNING, started_at=self._iso(now),
                trigger=TRIGGERED_CATCHUP if late else TRIGGERED_SCHEDULE,
                coalesced=skipped + 1,
            ),
            owner=self._id,
            lease_until=self._iso(now + timedelta(seconds=self._lease_s)),
            now=self._iso(now),
        )
        if run is None:
            return  # somebody else took this occurrence
        self._spawn(task, run)

    async def _skip(
        self, task: TaskRecord, scheduled: datetime, upcoming: datetime | None,
        next_due: datetime | None, now: datetime, *, status: str, coalesced: int, detail: str,
    ) -> None:
        run = self._new_run(
            task, scheduled, status, started_at="", finished_at=self._iso(now),
            coalesced=coalesced, detail=detail,
        )
        wrote = await self._store.record_skip(
            task_id=task.id, seen_due_at=task.due_at or "", run=run,
            next_run_at=to_iso(upcoming), due_at=to_iso(next_due), now=self._iso(now),
        )
        if wrote:
            logger.info("task %s %s: %s", task.id, status, detail)

    # -- running one occurrence ----------------------------------------------

    def _spawn(self, task: TaskRecord, run: TaskRunRecord) -> None:
        worker = asyncio.create_task(self._execute(task, run), name=f"sched:{run.run_id}")
        self._inflight[run.run_id] = worker
        worker.add_done_callback(lambda _: self._inflight.pop(run.run_id, None))

    async def _execute(self, task: TaskRecord, run: TaskRunRecord) -> None:
        deadline = float(task.deadline_s or self._run_deadline_s)
        started = self._now()
        work = asyncio.create_task(self._perform(task), name=f"sched-work:{run.run_id}")
        try:
            status, detail, result = await asyncio.wait_for(asyncio.shield(work), deadline)
        except TimeoutError:
            # Not cancelled on purpose: cancelling mid-prompt leaves the runtime
            # session in an unknown state, and in turn mode the reply still
            # belongs in the conversation. We stop WAITING, and say so.
            work.add_done_callback(
                lambda finished: logger.info(
                    "task %s: run %s finished after its deadline (%s)",
                    task.id, run.run_id, _outcome_of(finished),
                )
            )
            await self._finish(task, run, RUN_DEADLINE, started,
                               detail=f"still running after {deadline:.0f}s")
            return
        except asyncio.CancelledError:
            work.cancel()
            await self._finish(task, run, RUN_CANCELLED, started,
                               detail="the engine was shutting down")
            raise
        except Exception as exc:  # a bug in the dispatch path, not in the turn
            logger.exception("task %s: run %s crashed", task.id, run.run_id)
            await self._finish(task, run, RUN_ERROR, started,
                               detail=f"{type(exc).__name__}: {exc}"[:200])
            return
        await self._finish(task, run, status, started, detail=detail, result=result)

    async def _perform(self, task: TaskRecord) -> tuple[str, str, object | None]:
        """Do the work; return (status, detail, result). Only the dispatch
        failures are caught here — a turn reports its own ending."""
        if task.mode == MODE_PROMPT:
            try:
                result = await self._prompts.run_prompt(task.agent, task.prompt)
            except DispatchError as exc:
                return RUN_NO_AGENT, str(exc)[:200], None
            text = (result.text or "").strip()
            if not text:
                return RUN_EMPTY, "the run produced no text", result
            await self._notifier.deliver(
                task.agent, channel_id=task.channel_id,
                conversation_id=task.conversation_id, kind=task.kind, text=text,
            )
            return RUN_OK, "", result

        try:
            outcome = await self._dispatcher.run_turn(
                TurnRequest(
                    agent=task.agent, channel_id=task.channel_id,
                    conversation_id=task.conversation_id, kind=task.kind,
                    text=task.prompt, message_id=f"sched-{secrets.token_hex(6)}",
                    user_id=task.created_by,
                    username=task.created_by_username or "scheduler",
                )
            )
        except DispatchError as exc:
            return RUN_NO_AGENT, str(exc)[:200], None
        return _STATUS_BY_OUTCOME.get(outcome, RUN_ERROR), "", None

    async def _finish(
        self, task: TaskRecord, run: TaskRunRecord, status: str, started: datetime,
        *, detail: str = "", result: object | None = None,
    ) -> None:
        failed = status not in _SUCCEEDED
        # One more failure would reach the cap: stop burning turns on a task that
        # is plainly broken, and say so once.
        paused = failed and task.consecutive_failures + 1 >= self._max_failures
        release = STATE_PAUSED if paused else _released_state(task)
        await self._store.complete_run(
            run_id=run.run_id, task_id=task.id, status=status,
            finished_at=self._iso(), detail=detail,
            duration_ms=int((self._now() - started).total_seconds() * 1000),
            reply_chars=len(getattr(result, "text", "") or ""),
            tool_calls=len(getattr(result, "tool_calls", ()) or ()),
            failed=failed, release_state=release,
        )
        if paused:
            logger.warning(
                "task %s paused after %d failures in a row", task.id, self._max_failures
            )
        logger.info("task %s run %s: %s %s", task.id, run.run_id, status, detail)
        # Say it now rather than on the next tick: a run finishing at 09:00:01
        # should not have its failure announced twenty seconds later. The tick's
        # own sweep stays as the catch-up for anything this misses.
        await self._deliver_notices()

    # -- telling the user ----------------------------------------------------

    async def _deliver_notices(self) -> None:
        """Terminal runs nobody has been told about yet. Driven off the store
        rather than the run itself, so a notice owed by a crashed engine is still
        delivered by the next one."""
        for run in await self._store.unnotified_runs():
            task = await self._store.get_task(run.task_id)
            text = _notice_for(task, run, paused_at=self._max_failures)
            if task is not None and text:
                try:
                    await self._notifier.announce(
                        run.agent, channel_id=task.channel_id,
                        conversation_id=task.conversation_id, kind=task.kind, text=text,
                    )
                except Exception:
                    logger.warning("could not deliver the notice for run %s",
                                   run.run_id, exc_info=True)
                    continue
            await self._store.mark_notified(run.run_id)

    # -- bookkeeping ---------------------------------------------------------

    async def _write_heartbeat(self) -> None:
        nxt = await self._store.peek_next()
        tasks = await self._store.list_tasks()
        await self._store.write_heartbeat(  # type: ignore[attr-defined]
            SchedulerHeartbeat(
                scheduler_id=self._id, pid=os.getpid(), version=self._version,
                started_at=self._iso(self._started_at), last_tick_at=self._iso(),
                tick_seq=self._tick_seq, interval_s=self._tick_s,
                next_wake_at=nxt.next_run_at if nxt else None,
                next_task_id=nxt.id if nxt else "",
                next_task_name=nxt.name if nxt else "",
                running_count=len(self._inflight), tasks_total=len(tasks),
                last_error=self._last_error, last_error_at=self._last_error_at,
            )
        )

    def _note_error(self, exc: Exception) -> None:
        self._last_error = f"{type(exc).__name__}: {exc}"[:200]
        self._last_error_at = self._iso()

    def _iso(self, moment: datetime | None = None) -> str:
        return to_iso(moment or self._now()) or ""

    def _fresh_slack(self) -> float:
        """How late an occurrence may be and still count as punctual: one tick
        plus the task's own smear, since neither is a delay anyone chose."""
        return self._tick_s + 1.0

    def _within_startup_grace(self, now: datetime) -> bool:
        return (now - self._started_at).total_seconds() < self._startup_grace_s

    @staticmethod
    def _scheduled_instant(task: TaskRecord) -> datetime | None:
        return from_iso(task.next_run_at)

    def _new_run(
        self, task: TaskRecord, scheduled: datetime, status: str, *,
        started_at: str = "", finished_at: str = "", trigger: str = TRIGGERED_SCHEDULE,
        coalesced: int = 1, detail: str = "",
    ) -> TaskRunRecord:
        return TaskRunRecord(
            run_id=f"run_{secrets.token_hex(6)}", task_id=task.id, agent=task.agent,
            scheduled_at=self._iso(scheduled), started_at=started_at,
            finished_at=finished_at, status=status, trigger=trigger, owner=self._id,
            duration_ms=0, detail=detail, reply_chars=0, tool_calls=0,
            coalesced=max(1, coalesced), notified=0,
        )


# -- pure helpers --------------------------------------------------------------


def _trigger_of(task: TaskRecord) -> Trigger:
    anchor = from_iso(task.anchor_at) or utc_now()
    return Trigger(
        kind=task.trigger_kind, spec=task.trigger_spec, anchor_at=anchor,
        interval_s=task.interval_s, cron_expr=task.cron_expr, timezone=task.timezone,
    )


def _released_state(task: TaskRecord) -> str:
    """A one-shot is finished once it has run; anything else goes back to idle."""
    return STATE_IDLE if task.next_run_at else STATE_DONE


def _with_jitter(moment: datetime | None, jitter_s: int) -> datetime | None:
    return moment + timedelta(seconds=jitter_s) if moment is not None else None


def _short(delta: timedelta) -> str:
    """A duration a person can read at a glance."""
    seconds = int(abs(delta.total_seconds()))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h{(seconds % 3600) // 60:02d}m"
    return f"{seconds // 86400}d{(seconds % 86400) // 3600:02d}h"


def _outcome_of(finished: asyncio.Task) -> str:
    if finished.cancelled():
        return "cancelled"
    error = finished.exception()
    return f"{type(error).__name__}: {error}" if error else "finished"


def _notice_for(
    task: TaskRecord | None, run: TaskRunRecord, *, paused_at: int
) -> str:
    """What to tell the user about a finished run, or "" to stay quiet.

    Quiet on success unless asked otherwise, and quiet about the failures the
    turn itself already reported in the conversation — the point is that a
    failure is never silent, not that it is said twice."""
    if task is None:  # the task was deleted while its run was in flight
        return ""
    if task.notify == NOTIFY_NEVER:
        return ""
    label = f"«{task.name}»"
    if run.status in _SUCCEEDED:
        return f"⏰ Scheduled task {label} ran." if task.notify == NOTIFY_ALWAYS else ""
    if task.mode != MODE_PROMPT and run.status in _FLOW_REPORTED:
        return ""  # the reply (or the failure notice) is already in this thread

    reasons = {
        RUN_MISSED: f"⏰ Scheduled task {label} did not run: {run.detail}.",
        RUN_OVERLAP: f"⏰ Scheduled task {label} was skipped: {run.detail}.",
        RUN_DEADLINE: f"⏰ Scheduled task {label} hit its time limit: {run.detail}.",
        RUN_INTERRUPTED: f"⏰ Scheduled task {label} was cut short: {run.detail}.",
        RUN_NO_AGENT: f"⏰ Scheduled task {label} could not start: {run.detail}.",
        RUN_CANCELLED: f"⏰ Scheduled task {label} was cancelled: {run.detail}.",
    }
    text = reasons.get(run.status, f"⏰ Scheduled task {label} failed: {run.detail or run.status}.")
    if task.state == STATE_PAUSED and task.consecutive_failures >= paused_at:
        text += (
            f"\nPaused after {paused_at} failures in a row — "
            f"resume it with `impi task resume {task.id}`."
        )
    return text
