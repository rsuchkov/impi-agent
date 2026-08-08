"""The `/tasks` screen: what is scheduled, when it next runs, and what went wrong.

An engine screen, not an agent turn — listing a schedule and pausing it are facts
and edits, so a model in the loop would only add latency and a chance to invent a
task that isn't there. Every click redraws this one message.

State: ``page`` (which slice of the list) and ``task`` (the one being looked at).
``value`` carries what the last control returned.
"""

import logging
from collections.abc import Callable
from datetime import datetime

from crucible.interactions.screens import ScreenState, View, screen_action
from crucible.ports.chat.types import Card
from crucible.ports.tasks import TaskError
from crucible.scheduler.admin import TaskAdmin, local_time
from crucible.scheduler.health import ALIVE, liveness
from crucible.scheduler.triggers import from_iso, to_iso, utc_now
from crucible.store.base import (
    RUN_OK,
    STATE_PAUSED,
    STATE_RUNNING,
    SchedulerStateStore,
    TaskRecord,
    TaskStore,
)

logger = logging.getLogger(__name__)

# A page of 6: each task takes a card with three controls, and a message with
# more than that stops being scannable.
PAGE_SIZE = 6
RUNS_SHOWN = 5

_PREV, _NEXT, _BACK = "prev", "next", "back"
_OPEN, _PAUSE, _RESUME, _RUN_NOW, _DELETE = "open", "pause", "resume", "run", "delete"

# Mattermost paints the card's edge in these; Slack ignores them.
_ACCENT_HEADER = "#7a5299"
_ACCENT_HEALTHY = "#3db887"
_ACCENT_PAUSED = "#8e9297"
_ACCENT_FAILING = "#d24b4e"
_ACCENT_DETAIL = "#5d89ea"

DEFAULT_COMMAND = "tasks"


class TaskScreen:
    """``/tasks`` — every scheduled task, with the controls beside it."""

    def __init__(
        self,
        store: TaskStore,
        admin: TaskAdmin,
        *,
        heartbeat: SchedulerStateStore | None = None,
        command: str = DEFAULT_COMMAND,
        scheduler_enabled: bool = True,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self.command = command.lstrip("/").strip().lower() or DEFAULT_COMMAND
        self._store = store
        self._admin = admin
        # Where the ticker's proof of life is read from — the same concrete in
        # practice, but the screen only needs the one method.
        self._heartbeat = heartbeat
        self._enabled = scheduler_enabled
        self._now = clock

    async def render(self, state: ScreenState, *, user_id: str) -> View:
        value = str(state.data.get("value") or "")
        page = int(state.data.get("page") or 0)
        selected = str(state.data.get("task") or "")
        note = ""

        if value in (_PREV, _NEXT):
            page = max(0, page + (1 if value == _NEXT else -1))
            selected = ""
        elif value == _BACK:
            selected = ""
        elif ":" in value:
            verb, task_id = value.split(":", 1)
            if verb == _OPEN:
                selected = task_id
            else:
                note = await self._apply(verb, task_id)
                if verb == _DELETE:
                    selected = ""  # it is gone; go back to the list
                else:
                    selected = task_id

        state = state.with_data(page=page, task=selected, value="")
        if selected:
            return await self._detail(state, selected, note=note)
        return await self._index(state, page, note=note)

    # -- acting -------------------------------------------------------------

    async def _apply(self, verb: str, task_id: str) -> str:
        task = await self._store.get_task(task_id)
        if task is None:
            return "⚠️ that task is gone"
        try:
            if verb == _DELETE:
                await self._admin.cancel(task.agent, task.id)
                return f"🗑 **{task.name}** deleted — its history is kept"
            if verb == _RUN_NOW:
                # Asked, not run: the ticker owns firing, and it will pick this
                # up on its next pass.
                moved = await self._store.request_run_now(
                    task.id, now=to_iso(self._now()) or ""
                )
                return (
                    f"▶️ **{task.name}** is due now"
                    if moved
                    else f"⚠️ **{task.name}** is paused or already running"
                )
            paused = verb == _PAUSE
            await self._admin.set_paused(task.agent, task.id, paused)
            return f"{'⏸' if paused else '▶️'} **{task.name}** {'paused' if paused else 'resumed'}"
        except TaskError as exc:
            return f"⚠️ {exc}"

    # -- views --------------------------------------------------------------

    async def _index(self, state: ScreenState, page: int, *, note: str = "") -> View:
        tasks = await self._store.list_tasks()
        header = await self._header(len(tasks), page, tasks)
        if not tasks:
            return View.of(
                f"{header}\nNothing is scheduled yet. Ask an agent to remind you about "
                "something, or add one with `impi task add`."
                + (f"\n\n{note}" if note else ""),
                accent=_ACCENT_HEADER,
            )

        pages = max(1, -(-len(tasks) // PAGE_SIZE))
        page = min(page, pages - 1)
        window = tasks[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]
        base = state.with_data(page=page, task="", value="")

        controls = []
        if page > 0:
            controls.append(screen_action(base, id="prev", label="◀ Previous", value=_PREV))
        if page + 1 < pages:
            controls.append(screen_action(base, id="next", label="Next ▶", value=_NEXT))

        text = header + (f"  ·  page **{page + 1}** of **{pages}**" if pages > 1 else "")
        cards = [Card(text=text + (f"\n\n{note}" if note else ""),
                      actions=tuple(controls), accent=_ACCENT_HEADER)]
        for task in window:
            cards.append(
                Card(
                    text=_summary(task),
                    actions=tuple(self._task_controls(base, task)),
                    accent=_accent_of(task),
                )
            )
        return View(cards=tuple(cards))

    async def _detail(self, state: ScreenState, task_id: str, *, note: str = "") -> View:
        task = await self._store.get_task(task_id)
        if task is None:
            return await self._index(state.with_data(task=""), 0,
                                     note=note or "⚠️ that task is gone")
        base = state.with_data(task=task.id, value="")
        runs = await self._store.list_runs(task.id, limit=RUNS_SHOWN)

        lines = [
            f"### ⏰ {task.name}",
            f"`{task.id}`  ·  **{task.agent}**  ·  {task.state}",
            "",
            f"**Schedule** {task.trigger_spec} ({task.timezone or 'UTC'})",
            f"**Next run** {local_time(from_iso(task.next_run_at), task.timezone) or '—'}",
            f"**Mode** {task.mode}  ·  **If missed** {task.on_missed}  "
            f"·  **Notify** {task.notify}",
            f"**Ran** {task.run_count}×  ·  **missed** {task.miss_count}×"
            + (f"  ·  **{task.consecutive_failures} failures in a row**"
               if task.consecutive_failures else ""),
            "",
            f"> {task.prompt}",
        ]
        if runs:
            lines += ["", "**Recent runs**"]
            lines += [
                f"· {local_time(from_iso(run.scheduled_at), task.timezone)} — "
                f"**{run.status}**" + (f" ({run.detail})" if run.detail else "")
                for run in runs
            ]
        if note:
            lines += ["", note]

        controls = [
            screen_action(base, id="back", label="◀ All tasks", value=_BACK),
            *self._task_controls(base, task),
            screen_action(base, id="del", label="🗑 Delete", value=f"{_DELETE}:{task.id}",
                          style="danger"),
        ]
        return View.of("\n".join(lines), tuple(controls), accent=_ACCENT_DETAIL)

    def _task_controls(self, base: ScreenState, task: TaskRecord) -> list:
        paused = task.state == STATE_PAUSED
        return [
            screen_action(base, id=f"o{_key(task)}", label="Details",
                          value=f"{_OPEN}:{task.id}"),
            screen_action(
                base, id=f"p{_key(task)}",
                label="▶️ Resume" if paused else "⏸ Pause",
                value=f"{_RESUME if paused else _PAUSE}:{task.id}",
            ),
            screen_action(base, id=f"r{_key(task)}", label="Run now",
                          value=f"{_RUN_NOW}:{task.id}"),
        ]

    async def _header(self, total: int, page: int, tasks: list[TaskRecord]) -> str:
        """The list's title, with the scheduler's own state — a schedule is only
        as good as the loop behind it, and a dead loop must not look like a
        quiet one."""
        title = f"### ⏰ Scheduled tasks\n**{total}** scheduled"
        beat = await self._heartbeat.read_heartbeat() if self._heartbeat else None
        verdict, detail = liveness(beat, now=self._now(), enabled=self._enabled)
        if verdict == ALIVE:
            upcoming = min(
                (t.next_run_at for t in tasks if t.next_run_at), default=""
            )
            when = local_time(from_iso(upcoming), _zone_of(tasks, upcoming))
            return title + (f"  ·  next at **{when}**" if when else "  ·  nothing due")
        return f"{title}\n\n⚠️ **the scheduler is {verdict}** — {detail}"


# -- rendering helpers ---------------------------------------------------------


def _key(task: TaskRecord) -> str:
    """Action ids must be unique per message and alphanumeric (Mattermost's
    router drops anything else)."""
    return "".join(ch for ch in task.id if ch.isalnum())[-8:]


def _summary(task: TaskRecord) -> str:
    when = local_time(from_iso(task.next_run_at), task.timezone)
    last = task.last_status or "not run yet"
    mark = "⏸" if task.state == STATE_PAUSED else ("⏵" if task.state == STATE_RUNNING else "⏰")
    line = f"{mark} **{task.name}** · `{task.trigger_spec}` · {task.agent}"
    detail = f"next {when}" if when else "no next run"
    return f"{line}\n{detail}  ·  last: {last}"


def _accent_of(task: TaskRecord) -> str:
    if task.state == STATE_PAUSED:
        return _ACCENT_PAUSED
    if task.consecutive_failures or (task.last_status and task.last_status != RUN_OK):
        return _ACCENT_FAILING
    return _ACCENT_HEALTHY


def _zone_of(tasks: list[TaskRecord], next_run_at: str) -> str:
    return next((t.timezone for t in tasks if t.next_run_at == next_run_at), "")
