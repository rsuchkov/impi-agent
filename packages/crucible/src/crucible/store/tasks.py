"""Scheduled tasks, their run history and the scheduler's proof of life.

A mixin over the same connection and lock as the session inventory — one file,
one writer discipline, one set of conventions. It lives apart only so
``sessions.py`` stays readable.

Two methods are state machines rather than plain writes, and both rely on
sqlite3's implicit transactions (``isolation_level=""``): the statements between
a DML and ``commit()`` are one atomic unit, across processes too. The subtlety
that shapes the code: **a compare-and-swap UPDATE that matches nothing still
opens a transaction**, so every losing path must ``rollback()`` or it would hold
the write lock until someone else commits.
"""

import asyncio
import sqlite3
import threading
from dataclasses import fields

from crucible.store.base import (
    RUN_INTERRUPTED,
    RUN_RUNNING,
    STATE_IDLE,
    STATE_PAUSED,
    STATE_RUNNING,
    SchedulerHeartbeat,
    TaskRecord,
    TaskRunRecord,
)

_TASK_SCHEMA = """
-- Scheduled work. Timestamps are UTC ISO8601 (seconds), so string order is time
-- order. `timezone` is an IANA NAME, applied only when an occurrence is
-- computed. next_run_at/due_at are NULL — not '' — when there is no further
-- occurrence: '' compares <= every timestamp and would make a finished task
-- permanently due.
CREATE TABLE IF NOT EXISTS tasks (
  id                   TEXT PRIMARY KEY,
  agent                TEXT NOT NULL,
  name                 TEXT NOT NULL,
  channel_id           TEXT NOT NULL,
  conversation_id      TEXT NOT NULL,
  kind                 TEXT NOT NULL,
  mode                 TEXT NOT NULL,
  prompt               TEXT NOT NULL,
  trigger_kind         TEXT NOT NULL,
  trigger_spec         TEXT NOT NULL,
  interval_s           INTEGER NOT NULL DEFAULT 0,
  cron_expr            TEXT NOT NULL DEFAULT '',
  timezone             TEXT NOT NULL DEFAULT '',
  anchor_at            TEXT NOT NULL,
  next_run_at          TEXT,
  due_at               TEXT,
  jitter_s             INTEGER NOT NULL DEFAULT 0,
  state                TEXT NOT NULL,
  claim_owner          TEXT NOT NULL DEFAULT '',
  claim_at             TEXT NOT NULL DEFAULT '',
  lease_until          TEXT NOT NULL DEFAULT '',
  on_missed            TEXT NOT NULL DEFAULT 'run',
  notify               TEXT NOT NULL DEFAULT 'failures',
  deadline_s           INTEGER NOT NULL DEFAULT 0,
  created_by           TEXT NOT NULL DEFAULT '',
  created_by_username  TEXT NOT NULL DEFAULT '',
  created_at           TEXT NOT NULL,
  updated_at           TEXT NOT NULL,
  last_run_at          TEXT NOT NULL DEFAULT '',
  last_status          TEXT NOT NULL DEFAULT '',
  run_count            INTEGER NOT NULL DEFAULT 0,
  miss_count           INTEGER NOT NULL DEFAULT 0,
  consecutive_failures INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_tasks_due ON tasks (state, due_at);
CREATE UNIQUE INDEX IF NOT EXISTS idx_tasks_name ON tasks (agent, name);

-- One row per occurrence, ever. UNIQUE (task_id, scheduled_at) is the hard
-- idempotence guarantee behind the compare-and-swap claim: even with the CAS
-- lost, the second insert raises and the second firing is refused.
CREATE TABLE IF NOT EXISTS task_runs (
  run_id       TEXT PRIMARY KEY,
  task_id      TEXT NOT NULL,
  agent        TEXT NOT NULL,
  scheduled_at TEXT NOT NULL,
  started_at   TEXT NOT NULL DEFAULT '',
  finished_at  TEXT NOT NULL DEFAULT '',
  status       TEXT NOT NULL,
  trigger      TEXT NOT NULL DEFAULT 'schedule',
  owner        TEXT NOT NULL DEFAULT '',
  duration_ms  INTEGER NOT NULL DEFAULT 0,
  detail       TEXT NOT NULL DEFAULT '',
  reply_chars  INTEGER NOT NULL DEFAULT 0,
  tool_calls   INTEGER NOT NULL DEFAULT 0,
  coalesced    INTEGER NOT NULL DEFAULT 1,
  notified     INTEGER NOT NULL DEFAULT 0
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_runs_occurrence ON task_runs (task_id, scheduled_at);
CREATE INDEX IF NOT EXISTS idx_runs_task ON task_runs (task_id, scheduled_at DESC);
CREATE INDEX IF NOT EXISTS idx_runs_pending_notice ON task_runs (notified, finished_at);

-- Exactly one row: the scheduler's own state, so a reader can tell "nothing was
-- due" from "the timer is dead".
CREATE TABLE IF NOT EXISTS scheduler_heartbeat (
  id             INTEGER PRIMARY KEY CHECK (id = 1),
  scheduler_id   TEXT NOT NULL,
  pid            INTEGER NOT NULL,
  version        TEXT NOT NULL DEFAULT '',
  started_at     TEXT NOT NULL,
  last_tick_at   TEXT NOT NULL,
  tick_seq       INTEGER NOT NULL DEFAULT 0,
  interval_s     REAL NOT NULL,
  next_wake_at   TEXT,
  next_task_id   TEXT NOT NULL DEFAULT '',
  next_task_name TEXT NOT NULL DEFAULT '',
  running_count  INTEGER NOT NULL DEFAULT 0,
  tasks_total    INTEGER NOT NULL DEFAULT 0,
  last_error     TEXT NOT NULL DEFAULT '',
  last_error_at  TEXT NOT NULL DEFAULT ''
);
"""

# Column order comes from the dataclasses themselves, so SQL and records cannot
# drift — the records have too many fields for positional construction to stay
# honest by hand.
_TASK_FIELDS = tuple(f.name for f in fields(TaskRecord))
_TASK_COLUMNS = ", ".join(_TASK_FIELDS)
_TASK_PLACEHOLDERS = ", ".join("?" * len(_TASK_FIELDS))
_RUN_FIELDS = tuple(f.name for f in fields(TaskRunRecord))
_RUN_COLUMNS = ", ".join(_RUN_FIELDS)
_RUN_PLACEHOLDERS = ", ".join("?" * len(_RUN_FIELDS))
_BEAT_FIELDS = tuple(f.name for f in fields(SchedulerHeartbeat))
_BEAT_COLUMNS = ", ".join(_BEAT_FIELDS)


def _task(row: tuple) -> TaskRecord:
    return TaskRecord(**dict(zip(_TASK_FIELDS, row, strict=True)))


def _run(row: tuple) -> TaskRunRecord:
    return TaskRunRecord(**dict(zip(_RUN_FIELDS, row, strict=True)))


def _values(record: object, names: tuple[str, ...]) -> tuple:
    return tuple(getattr(record, name) for name in names)


class TaskStoreMixin:
    """The TaskStore/SchedulerStateStore facets of the SQLite store."""

    # Declared, not created: both belong to the store this is mixed into. The
    # annotations exist so the type checker knows the methods below may use
    # them — without them every `self._conn` here is an unknown attribute.
    _conn: sqlite3.Connection
    _lock: threading.Lock

    def _create_task_tables(self) -> None:
        """Create this facet's tables. The composing store calls it while it
        holds the lock on open, so the schema stays the mixin's own business."""
        self._conn.executescript(_TASK_SCHEMA)

    # -- reads ---------------------------------------------------------------

    def get_task_sync(self, task_id: str) -> TaskRecord | None:
        with self._lock:
            row = self._conn.execute(
                f"SELECT {_TASK_COLUMNS} FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
        return _task(row) if row else None

    def find_task_sync(self, agent: str, name: str) -> TaskRecord | None:
        with self._lock:
            row = self._conn.execute(
                f"SELECT {_TASK_COLUMNS} FROM tasks WHERE agent = ? AND name = ?",
                (agent, name),
            ).fetchone()
        return _task(row) if row else None

    def list_tasks_sync(
        self, agent: str | None = None, *, conversation_id: str | None = None
    ) -> list[TaskRecord]:
        where, args = [], []
        if agent:
            where.append("agent = ?")
            args.append(agent)
        if conversation_id:
            where.append("conversation_id = ?")
            args.append(conversation_id)
        clause = f" WHERE {' AND '.join(where)}" if where else ""
        with self._lock:
            rows = self._conn.execute(
                # id breaks ties: created_at has second resolution, and two tasks
                # made in the same second would otherwise come back in whatever
                # order the sorter felt like — which a paged view turns into a row
                # shown twice or not at all.
                f"SELECT {_TASK_COLUMNS} FROM tasks{clause} ORDER BY created_at, id",
                args,
            ).fetchall()
        return [_task(row) for row in rows]

    def due_tasks_sync(self, now: str, *, limit: int = 50) -> list[TaskRecord]:
        with self._lock:
            rows = self._conn.execute(
                f"SELECT {_TASK_COLUMNS} FROM tasks "
                "WHERE state IN (?, ?) AND due_at IS NOT NULL AND due_at <= ? "
                "ORDER BY due_at LIMIT ?",
                (STATE_IDLE, STATE_RUNNING, now, limit),
            ).fetchall()
        return [_task(row) for row in rows]

    def peek_next_sync(self) -> TaskRecord | None:
        with self._lock:
            row = self._conn.execute(
                f"SELECT {_TASK_COLUMNS} FROM tasks "
                "WHERE state = ? AND next_run_at IS NOT NULL "
                "ORDER BY next_run_at LIMIT 1",
                (STATE_IDLE,),
            ).fetchone()
        return _task(row) if row else None

    def list_runs_sync(self, task_id: str, *, limit: int = 20) -> list[TaskRunRecord]:
        with self._lock:
            rows = self._conn.execute(
                f"SELECT {_RUN_COLUMNS} FROM task_runs WHERE task_id = ? "
                "ORDER BY scheduled_at DESC LIMIT ?",
                (task_id, limit),
            ).fetchall()
        return [_run(row) for row in rows]

    def unnotified_runs_sync(self, *, limit: int = 20) -> list[TaskRunRecord]:
        with self._lock:
            rows = self._conn.execute(
                f"SELECT {_RUN_COLUMNS} FROM task_runs "
                "WHERE notified = 0 AND status != ? ORDER BY finished_at LIMIT ?",
                (RUN_RUNNING, limit),
            ).fetchall()
        return [_run(row) for row in rows]

    # -- admin ---------------------------------------------------------------

    def create_task_sync(self, task: TaskRecord) -> None:
        with self._lock:
            self._conn.execute(
                f"INSERT INTO tasks ({_TASK_COLUMNS}) VALUES ({_TASK_PLACEHOLDERS})",
                _values(task, _TASK_FIELDS),
            )
            self._conn.commit()

    def delete_task_sync(self, task_id: str) -> TaskRecord | None:
        with self._lock:
            row = self._conn.execute(
                f"SELECT {_TASK_COLUMNS} FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
            if row is None:
                return None
            # The ledger goes with the task. Keeping it would only accumulate
            # rows nothing can reach: every reader of a run looks the task up
            # first, so an orphaned history is invisible as well as unbounded.
            self._conn.execute("DELETE FROM task_runs WHERE task_id = ?", (task_id,))
            self._conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
            self._conn.commit()
        return _task(row)

    def set_paused_sync(
        self, task_id: str, paused: bool, *, now: str,
        next_run_at: str | None = None, due_at: str | None = None,
    ) -> bool:
        state = STATE_PAUSED if paused else STATE_IDLE
        with self._lock:
            cursor = self._conn.execute(
                "UPDATE tasks SET state = ?, next_run_at = ?, due_at = ?, updated_at = ? "
                "WHERE id = ? AND state != ?",
                (state, next_run_at, due_at, now, task_id, STATE_RUNNING),
            )
            self._conn.commit()
        return cursor.rowcount == 1

    def request_run_now_sync(self, task_id: str, *, now: str) -> bool:
        with self._lock:
            cursor = self._conn.execute(
                "UPDATE tasks SET next_run_at = ?, due_at = ?, updated_at = ? "
                "WHERE id = ? AND state = ?",
                (now, now, now, task_id, STATE_IDLE),
            )
            self._conn.commit()
        return cursor.rowcount == 1

    def reschedule_sync(
        self, task_id: str, *, next_run_at: str | None, due_at: str | None, now: str
    ) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE tasks SET next_run_at = ?, due_at = ?, updated_at = ? WHERE id = ?",
                (next_run_at, due_at, now, task_id),
            )
            self._conn.commit()

    # -- the atomic state machines -------------------------------------------

    def claim_due_sync(
        self, *, task_id: str, seen_due_at: str, next_run_at: str | None,
        due_at: str | None, run: TaskRunRecord, owner: str, lease_until: str, now: str,
    ) -> TaskRunRecord | None:
        with self._lock:
            # `due_at = seen_due_at` is the optimistic-concurrency token: nobody
            # can have advanced this schedule between our read and this write
            # without changing it.
            cursor = self._conn.execute(
                "UPDATE tasks SET state = ?, claim_owner = ?, claim_at = ?, "
                "lease_until = ?, next_run_at = ?, due_at = ?, last_run_at = ?, "
                "run_count = run_count + 1, updated_at = ? "
                "WHERE id = ? AND state = ? AND due_at IS NOT NULL AND due_at = ?",
                (STATE_RUNNING, owner, now, lease_until, next_run_at, due_at, now,
                 now, task_id, STATE_IDLE, seen_due_at),
            )
            if cursor.rowcount != 1:
                self._conn.rollback()  # a no-match UPDATE still opened one
                return None
            try:
                self._conn.execute(
                    f"INSERT INTO task_runs ({_RUN_COLUMNS}) VALUES ({_RUN_PLACEHOLDERS})",
                    _values(run, _RUN_FIELDS),
                )
            except sqlite3.IntegrityError:  # UNIQUE (task_id, scheduled_at)
                self._conn.rollback()
                return None
            self._conn.commit()
        return run

    def record_skip_sync(
        self, *, task_id: str, seen_due_at: str, run: TaskRunRecord,
        next_run_at: str | None, due_at: str | None, now: str,
    ) -> bool:
        with self._lock:
            # No lease: nothing is going to run. The task's own state is left
            # alone, so an overlap skip cannot un-claim the run in flight.
            cursor = self._conn.execute(
                "UPDATE tasks SET next_run_at = ?, due_at = ?, miss_count = miss_count + 1, "
                "last_status = ?, updated_at = ? "
                "WHERE id = ? AND due_at IS NOT NULL AND due_at = ?",
                (next_run_at, due_at, run.status, now, task_id, seen_due_at),
            )
            if cursor.rowcount != 1:
                self._conn.rollback()
                return False
            try:
                self._conn.execute(
                    f"INSERT INTO task_runs ({_RUN_COLUMNS}) VALUES ({_RUN_PLACEHOLDERS})",
                    _values(run, _RUN_FIELDS),
                )
            except sqlite3.IntegrityError:
                self._conn.rollback()
                return False
            self._conn.commit()
        return True

    def complete_run_sync(
        self, *, run_id: str, task_id: str, status: str, finished_at: str,
        duration_ms: int, detail: str = "", reply_chars: int = 0,
        tool_calls: int = 0, failed: bool = False, release_state: str = STATE_IDLE,
    ) -> None:
        with self._lock:
            # `status = running` guards against overwriting a verdict already
            # written by a reclaim: a run that came back after being declared
            # interrupted must not un-declare itself.
            cursor = self._conn.execute(
                "UPDATE task_runs SET status = ?, finished_at = ?, duration_ms = ?, "
                "detail = ?, reply_chars = ?, tool_calls = ? "
                "WHERE run_id = ? AND status = ?",
                (status, finished_at, duration_ms, detail, reply_chars, tool_calls,
                 run_id, RUN_RUNNING),
            )
            if cursor.rowcount != 1:
                self._conn.rollback()
                return
            streak = "consecutive_failures + 1" if failed else "0"
            self._conn.execute(
                f"UPDATE tasks SET state = ?, claim_owner = '', lease_until = '', "
                f"last_status = ?, consecutive_failures = {streak}, updated_at = ? "
                "WHERE id = ?",
                (release_state, status, finished_at, task_id),
            )
            self._conn.commit()

    def reclaim_orphans_sync(self, *, scheduler_id: str, now: str) -> list[TaskRunRecord]:
        with self._lock:
            rows = self._conn.execute(
                f"SELECT {_RUN_COLUMNS} FROM task_runs WHERE status = ?", (RUN_RUNNING,)
            ).fetchall()
            orphans = [
                _run(row) for row in rows
                if _run(row).owner != scheduler_id or _lease_expired(self._conn, _run(row), now)
            ]
            if not orphans:
                self._conn.rollback()
                return []
            for orphan in orphans:
                self._conn.execute(
                    "UPDATE task_runs SET status = ?, finished_at = ?, detail = ? "
                    "WHERE run_id = ?",
                    (RUN_INTERRUPTED, now,
                     "the engine stopped while this run was in flight", orphan.run_id),
                )
                self._conn.execute(
                    "UPDATE tasks SET state = ?, claim_owner = '', lease_until = '', "
                    "last_status = ?, consecutive_failures = consecutive_failures + 1, "
                    "updated_at = ? WHERE id = ? AND state = ?",
                    (STATE_IDLE, RUN_INTERRUPTED, now, orphan.task_id, STATE_RUNNING),
                )
            self._conn.commit()
        return orphans

    def mark_notified_sync(self, run_id: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE task_runs SET notified = 1 WHERE run_id = ?", (run_id,)
            )
            self._conn.commit()

    def prune_runs_sync(self, *, keep_per_task: int, before: str) -> int:
        with self._lock:
            cursor = self._conn.execute(
                "DELETE FROM task_runs WHERE finished_at != '' AND finished_at < ? "
                "AND run_id NOT IN ("
                "  SELECT run_id FROM ("
                "    SELECT run_id, ROW_NUMBER() OVER ("
                "      PARTITION BY task_id ORDER BY scheduled_at DESC) AS rank"
                "    FROM task_runs"
                "  ) WHERE rank <= ?)",
                (before, keep_per_task),
            )
            self._conn.commit()
        return cursor.rowcount

    # -- the scheduler's own state -------------------------------------------

    def write_heartbeat_sync(self, beat: SchedulerHeartbeat) -> None:
        assignments = ", ".join(f"{name} = excluded.{name}" for name in _BEAT_FIELDS)
        with self._lock:
            self._conn.execute(
                f"INSERT INTO scheduler_heartbeat (id, {_BEAT_COLUMNS}) "
                f"VALUES (1, {', '.join('?' * len(_BEAT_FIELDS))}) "
                f"ON CONFLICT (id) DO UPDATE SET {assignments}",
                _values(beat, _BEAT_FIELDS),
            )
            self._conn.commit()

    def read_heartbeat_sync(self) -> SchedulerHeartbeat | None:
        with self._lock:
            row = self._conn.execute(
                f"SELECT {_BEAT_COLUMNS} FROM scheduler_heartbeat WHERE id = 1"
            ).fetchone()
        return SchedulerHeartbeat(**dict(zip(_BEAT_FIELDS, row, strict=True))) if row else None

    # -- async facade (TaskStore / SchedulerStateStore ports) ------------------

    async def create_task(self, task: TaskRecord) -> None:
        await asyncio.to_thread(self.create_task_sync, task)

    async def get_task(self, task_id: str) -> TaskRecord | None:
        return await asyncio.to_thread(self.get_task_sync, task_id)

    async def find_task(self, agent: str, name: str) -> TaskRecord | None:
        return await asyncio.to_thread(self.find_task_sync, agent, name)

    async def list_tasks(
        self, agent: str | None = None, *, conversation_id: str | None = None
    ) -> list[TaskRecord]:
        return await asyncio.to_thread(
            self.list_tasks_sync, agent, conversation_id=conversation_id
        )

    async def due_tasks(self, now: str, *, limit: int = 50) -> list[TaskRecord]:
        return await asyncio.to_thread(self.due_tasks_sync, now, limit=limit)

    async def peek_next(self) -> TaskRecord | None:
        return await asyncio.to_thread(self.peek_next_sync)

    async def delete_task(self, task_id: str) -> TaskRecord | None:
        return await asyncio.to_thread(self.delete_task_sync, task_id)

    async def set_paused(
        self, task_id: str, paused: bool, *, now: str,
        next_run_at: str | None = None, due_at: str | None = None,
    ) -> bool:
        return await asyncio.to_thread(
            self.set_paused_sync, task_id, paused, now=now,
            next_run_at=next_run_at, due_at=due_at,
        )

    async def request_run_now(self, task_id: str, *, now: str) -> bool:
        return await asyncio.to_thread(self.request_run_now_sync, task_id, now=now)

    async def reschedule(
        self, task_id: str, *, next_run_at: str | None, due_at: str | None, now: str
    ) -> None:
        await asyncio.to_thread(
            self.reschedule_sync, task_id, next_run_at=next_run_at, due_at=due_at, now=now
        )

    async def claim_due(
        self, *, task_id: str, seen_due_at: str, next_run_at: str | None,
        due_at: str | None, run: TaskRunRecord, owner: str, lease_until: str, now: str,
    ) -> TaskRunRecord | None:
        return await asyncio.to_thread(
            self.claim_due_sync, task_id=task_id, seen_due_at=seen_due_at,
            next_run_at=next_run_at, due_at=due_at, run=run, owner=owner,
            lease_until=lease_until, now=now,
        )

    async def record_skip(
        self, *, task_id: str, seen_due_at: str, run: TaskRunRecord,
        next_run_at: str | None, due_at: str | None, now: str,
    ) -> bool:
        return await asyncio.to_thread(
            self.record_skip_sync, task_id=task_id, seen_due_at=seen_due_at, run=run,
            next_run_at=next_run_at, due_at=due_at, now=now,
        )

    async def complete_run(
        self, *, run_id: str, task_id: str, status: str, finished_at: str,
        duration_ms: int, detail: str = "", reply_chars: int = 0,
        tool_calls: int = 0, failed: bool = False, release_state: str = STATE_IDLE,
    ) -> None:
        await asyncio.to_thread(
            self.complete_run_sync, run_id=run_id, task_id=task_id, status=status,
            finished_at=finished_at, duration_ms=duration_ms, detail=detail,
            reply_chars=reply_chars, tool_calls=tool_calls, failed=failed,
            release_state=release_state,
        )

    async def reclaim_orphans(
        self, *, scheduler_id: str, now: str
    ) -> list[TaskRunRecord]:
        return await asyncio.to_thread(
            self.reclaim_orphans_sync, scheduler_id=scheduler_id, now=now
        )

    async def list_runs(self, task_id: str, *, limit: int = 20) -> list[TaskRunRecord]:
        return await asyncio.to_thread(self.list_runs_sync, task_id, limit=limit)

    async def unnotified_runs(self, *, limit: int = 20) -> list[TaskRunRecord]:
        return await asyncio.to_thread(self.unnotified_runs_sync, limit=limit)

    async def mark_notified(self, run_id: str) -> None:
        await asyncio.to_thread(self.mark_notified_sync, run_id)

    async def prune_runs(self, *, keep_per_task: int, before: str) -> int:
        return await asyncio.to_thread(
            self.prune_runs_sync, keep_per_task=keep_per_task, before=before
        )

    async def write_heartbeat(self, beat: SchedulerHeartbeat) -> None:
        await asyncio.to_thread(self.write_heartbeat_sync, beat)

    async def read_heartbeat(self) -> SchedulerHeartbeat | None:
        return await asyncio.to_thread(self.read_heartbeat_sync)


def _lease_expired(conn: sqlite3.Connection, run: TaskRunRecord, now: str) -> bool:
    """Whether the task behind this run has let its lease run out. A live run of
    OUR scheduler keeps its lease fresh by definition of the deadline being
    shorter, so this only catches genuinely abandoned work."""
    row = conn.execute("SELECT lease_until FROM tasks WHERE id = ?", (run.task_id,)).fetchone()
    if row is None:
        return True  # the task is gone; its run cannot finish
    return bool(row[0]) and row[0] <= now
