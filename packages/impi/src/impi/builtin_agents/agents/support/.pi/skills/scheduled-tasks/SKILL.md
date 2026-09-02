---
name: scheduled-tasks
description: Set up recurring or delayed agent work, and explain why a scheduled run did not happen. Use when the operator asks to schedule/pause/cancel a task for an agent, or reports that a task did not run, ran twice, ran at the wrong time, or was paused by itself.
---

# Scheduled work

A task is a prompt plus a schedule, owned by one agent in one conversation, kept
in the engine's database. A ticker inside the engine claims each occurrence and
records a row for **every** one — including the ones that did not run, with the
reason. Reference: `$IMPI_ROOT/docs/tasks.md`.

Your own `schedule_task` / `list_tasks` / `cancel_task` / `pause_task` tools act
on **your** tasks only. Another agent's schedule is reached with the `impi task`
CLI through `bash` — that is the tool for almost everything in this skill.

When somebody wants to SEE the tasks rather than have them described, use
`open_screen` with `tasks`: the engine posts its own browser and answers every
button on it, so what they get is the live list rather than a paraphrase of one.
The tool itself tells you how to speak around it.

## 1. Writing a schedule

| Form | Example | Notes |
|---|---|---|
| delay | `in 2h`, `in 45m` | one-off |
| moment | `2026-08-10T09:00` | one-off, in the task's zone |
| interval | `every 15m`, `every 24h` | absolute duration; minimum 60s |
| cron | `0 9 * * 1-5` | five fields, wall-clock in the task's zone |

An interval is an absolute duration; a cron keeps its wall-clock time across a
daylight-saving change. **The container runs in UTC** — a schedule with no zone
means UTC, so `0 9 * * 1-5` is nine in the morning UTC unless
`SCHEDULER_TIMEZONE` is set or the task names its own (`--tz`). This is the most
common complaint ("it ran at the wrong time") and the first thing to check.

Two run modes: `turn` (default) is an ordinary turn in that conversation, with
its memory; `prompt` is a fresh memoryless run whose answer the engine posts.

## 2. Managing a task (any agent)

```bash
impi task list [--agent <agent>]      # everything, with the next run time
impi task show <task>                 # prompt, settings, counters, its zone
impi task runs <task> [--limit N]     # every occurrence and why it ended so
impi task add --agent <agent> --conversation <id> \
    --name <name> --prompt "<what to do>" --schedule "0 9 * * 1-5" \
    [--tz Europe/Belgrade] [--mode turn|prompt] \
    [--notify failures|always|never] [--on-missed run|skip]
impi task pause <task>                # ...and resume
impi task run-now <task>              # ask the engine to fire it at once
impi task rm <task> --yes             # the task and its run history
impi task status                      # is the scheduler alive, what is next
```

`--conversation` must be a conversation the agent already has — a task belongs
somewhere, and the error lists the ids it knows. `impi sessions list` shows them.
Pass `--yes` to `rm`: with no terminal to ask on it refuses rather than deleting.

`run-now` only moves the schedule; the **engine** fires it, within one tick. A
CLI container has no gateways and can never run a turn itself.

Pausing clears the next run; resuming recomputes it on the task's original
phase, not from now.

## 3. Why a run did not happen

Ask, don't guess — an idle ticker and a dead one look the same from outside:

```bash
impi task status        # alive / stale / never / absent, and the next wake-up
impi task runs <task>   # the per-occurrence ledger
```

Scheduler verdicts: **alive**; **stale** = it stopped ticking (look in the log,
the heartbeat carries the last error); **never** = no tick was ever recorded
against this database; **absent** = `SCHEDULER_ENABLED=false`, off on purpose,
not broken.

Run statuses and what each one means:

| Status | What happened |
|---|---|
| `ok` | the agent answered |
| `empty` | the turn produced neither text nor a tool call |
| `timeout` | the runtime timed out; the user already got the fallback message |
| `error` | the runtime or the dispatch path failed |
| `deadline` | the engine stopped waiting (`SCHEDULER_RUN_DEADLINE_S`); the turn may still be running |
| `interrupted` | the engine died mid-run; the occurrence is **not** re-run |
| `no_agent` | the agent isn't live — no profile, or no token |
| `no_conversation` | nowhere to post the result |
| `missed` | later than its grace window, or the task has `on_missed: skip` |
| `overlap` | the previous run outlasted the task's own period |
| `cancelled` | shutdown, or the task was removed mid-run |
| `duplicate` | the turn was deduped |

**Missed runs.** If the engine was down at the due time, the task catches up
**once** — never once per missed interval — and only inside its grace window:
half the period, clamped to 2 minutes…2 hours (15 minutes for a one-off). Later
than that it is recorded as `missed` with how late it was, and the schedule
moves on. There is also a 60-second pause after startup before anything is
caught up.

**A task that paused itself** hit `SCHEDULER_MAX_FAILURES` (5) failures in a
row; it said so in its conversation. Fix the cause, then
`impi task resume <task>`.

Failures are reported to the conversation the task belongs to. In `turn` mode
the flow already told the user about a timeout or error, so the scheduler stays
quiet about those rather than saying it twice. Per-task `notify` chooses
`failures` (default), `always` or `never`.

## 4. Giving an agent the ability to schedule

The scheduling tools are capability-gated on `SCHEDULER_ENABLED`. To let an
agent create its own tasks, add `schedule_task` (and any of `list_tasks`,
`pause_task`, `cancel_task`) to its `runtime.tools` and reload. Those tools are
scoped to that agent and its conversation — they cannot touch another agent's
schedule.
