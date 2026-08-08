# Scheduled and recurring tasks

"Remind me in two hours." "Every weekday at nine, summarize what came in
overnight." A task is a prompt plus a schedule, kept in the engine's database and
run in the conversation it was created in.

## Creating one

**In chat.** Ask the agent; it has `schedule_task` and answers with the next few
fire times, so a misunderstanding surfaces immediately:

> — every weekday at 9, go through my inbox and tell me what needs an answer
>
> — Done. **inbox** runs at 09:00 on weekdays (Europe/Belgrade). Next three:
>   Mon 10 Aug 09:00, Tue 11 Aug 09:00, Wed 12 Aug 09:00.

It also has `list_tasks`, `pause_task` and `cancel_task`. An agent only ever sees
and touches **its own** tasks.

**From a terminal.** `impi task add` needs the conversation named outright — the
CLI takes part in no turn, so it cannot guess which one you mean:

```bash
impi task list                      # everything, with the next run time
impi task add --agent assistant --conversation dm1 \
    --name inbox --prompt "go through my inbox" --schedule "0 9 * * 1-5"
impi task show inbox                # the prompt, the settings, the counters
impi task runs inbox                # what happened, and why
impi task pause inbox               # …and `resume`
impi task run-now inbox             # ask the engine to run it at once
impi task rm inbox                  # the task goes; its history stays
impi task status                    # is the scheduler alive, what is next
```

**In chat, with buttons.** `/tasks` lists everything with pause/resume, run-now
and details beside each one. Register the slash command the same way as
`/skills` (see [commands.md](commands.md)); rename it with `TASKS_COMMAND` if the
word is taken.

## Writing a schedule

| Form | Example | Means |
|---|---|---|
| delay | `in 2h`, `90m`, `1h30m` | once, that far from now |
| moment | `2026-08-09T09:00` | once, then; a bare time is read in the task's zone |
| interval | `every 15m`, `every 1d` | from now on, that often (at least 60s) |
| cron | `0 9 * * 1-5`, `cron: */30 * * * *` | five fields, in the task's zone |

Times belong to a **zone**, not to the server: pass `timezone` (an IANA name like
`Europe/Belgrade`) or set `SCHEDULER_TIMEZONE` once. The container itself runs in
UTC, which is why a default matters.

A cron expression keeps its wall-clock time across a daylight-saving change —
`0 9 * * *` is 09:00 in both halves of the year. An interval is an absolute
duration, so `every 24h` stays 24 hours and its local time shifts. On the day the
clocks go forward, a time that does not exist (02:30, say) runs at the end of the
gap; on the day they go back, a time that happens twice runs once.

## What a run does

Two modes, chosen per task:

- **`turn`** (default) — the prompt arrives as an ordinary turn in the task's
  conversation, with that conversation's memory and the agent's tools. The reply
  lands there, and you can just answer it.
- **`prompt`** — a fresh run with no history, whose answer the scheduler posts.
  Cheaper and repeatable; use it for polls and digests that shouldn't drag the
  whole thread into the model.

Tasks are spread out by a small per-task offset derived from the task's id, so a
dozen tasks written `0 9 * * *` don't all spawn a subprocess on the same second.
The listing shows the honest time, not the offset one.

## When something goes wrong

A failure is never silent, and never said twice. In `turn` mode the agent's own
turn already posts about a timeout or an error, so the scheduler stays quiet
about those; everything else it reports itself:

| The run says | What happened |
|---|---|
| `ok` | the agent answered (or posted a widget — that IS the answer) |
| `empty` | the turn produced neither text nor an action |
| `timeout` | the runtime ran out of time; you got the usual fallback message |
| `error` | the runtime failed |
| `deadline` | it outlived `SCHEDULER_RUN_DEADLINE_S`; the turn may still finish |
| `interrupted` | the engine stopped mid-run — reported after it comes back |
| `no_agent` | the agent is not running: no profile, or no token |
| `no_conversation` | there was nowhere to post the result |
| `missed` | too late to be worth running, or the task said not to catch up |
| `overlap` | the previous run was still going when this one came due |
| `cancelled` | shut down, or the task was removed mid-run |

Per task, `notify` chooses how loud this is: `failures` (default), `always` (also
confirm good runs) or `never`. After **five failures in a row** a task is paused
and says so once — a broken task should stop burning turns.

**Missed runs.** If the engine was down at the due time, the task catches up
**once** — never once per missed interval — and only if the delay is within its
grace window (half the period, between two minutes and two hours; fifteen minutes
for a one-off). Later than that it is recorded as `missed`, with the reason, and
moves on to the next occurrence. A task where a late run is worse than no run can
say `on_missed: skip`. Nothing catches up in the first minute after a restart,
so a restart cannot turn into a burst.

## What survives what

- **A restart mid-run.** The run row exists before the work starts, so the next
  start finds it, marks it `interrupted` and tells you. The occurrence is not
  re-run: a turn may already have sent an email through a tool, and a silent
  repeat is worse than an honest report.
- **Two engines on one database.** A task is claimed with a compare-and-swap and
  the store refuses a second row for the same occurrence, so nothing fires twice.
- **`impi update`.** Tasks and their history live in the same volume as the rest
  of the engine's state.

## Is it running?

An idle scheduler and a dead one look the same from outside, so the ticker
records a heartbeat at the end of every pass:

```bash
impi task status
# ✔ scheduler alive: tick #421 6s ago, 0 run(s) in flight; next inbox at 2026-08-10 09:00
```

`impi doctor` asks the same question, and `/tasks` puts the answer above the
list. `never` means no tick was ever recorded, `stale` that the loop stopped,
`absent` that `SCHEDULER_ENABLED` is off — off on purpose is not a failure.

## Settings

| Variable | Default | What it does |
|---|---|---|
| `SCHEDULER_ENABLED` | `true` | off = no ticker, and no agent gets the scheduling tools |
| `SCHEDULER_TICK_S` | `20` | how often due work is looked for |
| `SCHEDULER_TIMEZONE` | `UTC` | the zone a task's schedule is read in unless it names one |
| `SCHEDULER_MAX_CONCURRENT` | `2` | scheduled runs at once (the runtime allows 4 sessions in total) |
| `SCHEDULER_RUN_DEADLINE_S` | `900` | stop waiting on a run after this |
| `SCHEDULER_STARTUP_GRACE_S` | `60` | let the gateways log in before catching anything up |
| `SCHEDULER_MAX_FAILURES` | `5` | failures in a row before a task is paused |
| `SCHEDULER_MAX_TASKS_PER_AGENT` | `50` | a limit on how many an agent may create |
| `TASKS_COMMAND` | `tasks` | the slash command `/tasks` binds to |
