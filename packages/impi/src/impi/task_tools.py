"""Scheduling from inside a turn: "remind me at six", "every weekday at nine".

One tool per verb, like the rest of the catalogue. A task always belongs to the
agent that created it and to the conversation the call ran in, so an agent can
neither schedule work for someone else nor touch another agent's tasks.
"""

from typing import Any, ClassVar

from crucible.ports.tasks import TaskError, TaskView
from crucible.store.base import MODE_TURN, MODES, NOTIFY_MODES
from crucible.tools.base import CAP_SCHEDULER, Tool, ToolContext, ToolError
from crucible.tools.registry import tool


def _require_str(args: dict[str, Any], key: str) -> str:
    value = str(args.get(key) or "").strip()
    if not value:
        raise ToolError(f"{key} is required")
    return value


def _describe(view: TaskView) -> dict[str, Any]:
    described: dict[str, Any] = {
        "id": view.id, "name": view.name, "schedule": view.schedule,
        "mode": view.mode, "state": view.state, "timezone": view.timezone,
        "next_run": view.next_run, "last_run": view.last_run,
        "last_status": view.last_status, "notify": view.notify,
    }
    if view.upcoming:
        described["upcoming"] = list(view.upcoming)
    return described


@tool
class ScheduleTask(Tool):
    name: ClassVar[str] = "schedule_task"
    requires: ClassVar[frozenset[str]] = frozenset({CAP_SCHEDULER})
    description: ClassVar[str] = (
        "Schedule work for later in THIS conversation: a one-off reminder, or "
        "something recurring. `schedule` accepts a delay ('in 2h'), a moment "
        "('2026-08-09T09:00'), an interval ('every 15m', at least 60s) or a "
        "5-field cron expression ('0 9 * * 1-5'). When the time comes, `prompt` "
        "is put to you as if the person had written it. The answer lists the "
        "next few fire times — read them back to the person so a "
        "misunderstanding surfaces now rather than at 3am."
    )
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Short name, unique among your tasks"},
            "prompt": {"type": "string", "description": "What you should do when it fires"},
            "schedule": {"type": "string", "description": "Delay, moment, interval or cron"},
            "timezone": {
                "type": "string",
                "description": "IANA zone the schedule is written in "
                "(e.g. Europe/Belgrade); omit for the engine default",
            },
            "mode": {
                "type": "string", "enum": list(MODES),
                "description": "turn (default): a normal turn here, with this "
                "conversation's memory. prompt: a fresh run with no history, "
                "whose answer is posted here",
            },
            "notify": {
                "type": "string", "enum": list(NOTIFY_MODES),
                "description": "failures (default): speak up only when a run "
                "cannot happen. always: confirm every run. never: stay silent",
            },
            "on_missed": {
                "type": "string", "enum": ["run", "skip"],
                "description": "If the engine was down at the due time: run "
                "(default) catches up once if it is still worth it; skip only reports it",
            },
        },
        "required": ["name", "prompt", "schedule"],
    }

    async def execute(self, ctx: ToolContext, args: dict[str, Any]) -> Any:
        try:
            view = await ctx.require_tasks().schedule(
                ctx.agent_name, ctx.runtime_session_id,
                name=_require_str(args, "name"),
                prompt=_require_str(args, "prompt"),
                schedule=_require_str(args, "schedule"),
                mode=str(args.get("mode") or MODE_TURN),
                timezone=str(args.get("timezone") or ""),
                notify=str(args.get("notify") or "failures"),
                on_missed=str(args.get("on_missed") or "run"),
            )
        except TaskError as exc:
            raise ToolError(str(exc)) from exc
        return {"scheduled": True, **_describe(view)}


@tool
class ListTasks(Tool):
    name: ClassVar[str] = "list_tasks"
    requires: ClassVar[frozenset[str]] = frozenset({CAP_SCHEDULER})
    description: ClassVar[str] = (
        "Your scheduled tasks: when each next runs, and how the last run went."
    )
    parameters: ClassVar[dict[str, Any]] = {"type": "object", "properties": {}}

    async def execute(self, ctx: ToolContext, args: dict[str, Any]) -> Any:
        views = await ctx.require_tasks().list_tasks(ctx.agent_name)
        return {"tasks": [_describe(view) for view in views]}


@tool
class CancelTask(Tool):
    name: ClassVar[str] = "cancel_task"
    requires: ClassVar[frozenset[str]] = frozenset({CAP_SCHEDULER})
    description: ClassVar[str] = (
        "Delete one of your scheduled tasks, by name or id. Its history is kept."
    )
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {"task": {"type": "string", "description": "Task name or id"}},
        "required": ["task"],
    }

    async def execute(self, ctx: ToolContext, args: dict[str, Any]) -> Any:
        try:
            view = await ctx.require_tasks().cancel(ctx.agent_name, _require_str(args, "task"))
        except TaskError as exc:
            raise ToolError(str(exc)) from exc
        return {"cancelled": True, "id": view.id, "name": view.name}


@tool
class PauseTask(Tool):
    name: ClassVar[str] = "pause_task"
    requires: ClassVar[frozenset[str]] = frozenset({CAP_SCHEDULER})
    description: ClassVar[str] = (
        "Pause one of your tasks, or resume a paused one (`resume: true`). A "
        "resumed task returns to its original rhythm rather than starting over."
    )
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "task": {"type": "string", "description": "Task name or id"},
            "resume": {"type": "boolean", "description": "Resume instead of pausing"},
        },
        "required": ["task"],
    }

    async def execute(self, ctx: ToolContext, args: dict[str, Any]) -> Any:
        paused = not bool(args.get("resume"))
        try:
            view = await ctx.require_tasks().set_paused(
                ctx.agent_name, _require_str(args, "task"), paused
            )
        except TaskError as exc:
            raise ToolError(str(exc)) from exc
        return {"paused": paused, **_describe(view)}
