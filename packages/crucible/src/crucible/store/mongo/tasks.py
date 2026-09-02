"""The TaskStore / SchedulerStateStore facets of the MongoDB backend.

The claim protocol is the whole reason this file needs care. SQLite advances the
schedule and opens the run row inside one transaction; a standalone mongod has
no transaction to offer, so the same guarantee is bought a different way — see
``claim_due``.
"""

from __future__ import annotations

from pymongo.errors import DuplicateKeyError

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
from crucible.store.mongo.base import (
    HEARTBEAT,
    HEARTBEAT_ID,
    RUNS,
    TASKS,
    MongoBase,
    from_doc,
    to_doc,
)


class MongoTaskMixin(MongoBase):
    """Scheduled work and its run history, on Mongo."""

    # -- reads ----------------------------------------------------------------

    async def get_task(self, task_id: str) -> TaskRecord | None:
        db = await self._ready()
        doc = await db[TASKS].find_one({"_id": task_id})
        return from_doc(TaskRecord, doc) if doc else None

    async def find_task(self, agent: str, name: str) -> TaskRecord | None:
        db = await self._ready()
        doc = await db[TASKS].find_one({"agent": agent, "name": name})
        return from_doc(TaskRecord, doc) if doc else None

    async def list_tasks(
        self, agent: str | None = None, *, conversation_id: str | None = None
    ) -> list[TaskRecord]:
        db = await self._ready()
        query: dict = {}
        if agent:
            query["agent"] = agent
        if conversation_id:
            query["conversation_id"] = conversation_id
        # _id IS the task id, so this is the same tiebreaker SQLite uses:
        # created_at has second resolution and two tasks made in one second
        # would otherwise come back in whatever order the sorter felt like.
        cursor = db[TASKS].find(query).sort([("created_at", 1), ("_id", 1)])
        return [from_doc(TaskRecord, doc) async for doc in cursor]

    async def due_tasks(self, now: str, *, limit: int = 50) -> list[TaskRecord]:
        db = await self._ready()
        cursor = (
            db[TASKS]
            .find({
                "state": {"$in": [STATE_IDLE, STATE_RUNNING]},
                # A task with no schedule has due_at null. Mongo's comparison
                # operators bracket by type, so null would not match `$lte` on a
                # string anyway — the `$ne` says out loud what we are relying on.
                "due_at": {"$ne": None, "$lte": now},
            })
            .sort([("due_at", 1)])
            .limit(limit)
        )
        return [from_doc(TaskRecord, doc) async for doc in cursor]

    async def peek_next(self) -> TaskRecord | None:
        db = await self._ready()
        doc = await db[TASKS].find_one(
            {"state": STATE_IDLE, "next_run_at": {"$ne": None}},
            sort=[("next_run_at", 1)],
        )
        return from_doc(TaskRecord, doc) if doc else None

    async def list_runs(self, task_id: str, *, limit: int = 20) -> list[TaskRunRecord]:
        db = await self._ready()
        cursor = (
            db[RUNS].find({"task_id": task_id})
            .sort([("scheduled_at", -1)]).limit(limit)
        )
        return [from_doc(TaskRunRecord, doc) async for doc in cursor]

    async def unnotified_runs(self, *, limit: int = 20) -> list[TaskRunRecord]:
        db = await self._ready()
        cursor = (
            db[RUNS].find({"notified": 0, "status": {"$ne": RUN_RUNNING}})
            .sort([("finished_at", 1)]).limit(limit)
        )
        return [from_doc(TaskRunRecord, doc) async for doc in cursor]

    # -- admin ----------------------------------------------------------------

    async def create_task(self, task: TaskRecord) -> None:
        db = await self._ready()
        await db[TASKS].insert_one(to_doc(task, _id=task.id))

    async def delete_task(self, task_id: str) -> TaskRecord | None:
        db = await self._ready()
        doc = await db[TASKS].find_one_and_delete({"_id": task_id})
        if doc is None:
            return None
        # The history goes with the task: every reader of a run looks the task
        # up first, so an orphaned run is invisible as well as unbounded.
        await db[RUNS].delete_many({"task_id": task_id})
        return from_doc(TaskRecord, doc)

    async def set_paused(
        self, task_id: str, paused: bool, *, now: str,
        next_run_at: str | None = None, due_at: str | None = None,
    ) -> bool:
        db = await self._ready()
        result = await db[TASKS].update_one(
            {"_id": task_id, "state": {"$ne": STATE_RUNNING}},
            {"$set": {
                "state": STATE_PAUSED if paused else STATE_IDLE,
                "next_run_at": next_run_at, "due_at": due_at, "updated_at": now,
            }},
        )
        return result.matched_count == 1

    async def request_run_now(self, task_id: str, *, now: str) -> bool:
        db = await self._ready()
        result = await db[TASKS].update_one(
            {"_id": task_id, "state": STATE_IDLE},
            {"$set": {"next_run_at": now, "due_at": now, "updated_at": now}},
        )
        return result.matched_count == 1

    async def reschedule(
        self, task_id: str, *, next_run_at: str | None, due_at: str | None, now: str
    ) -> None:
        db = await self._ready()
        await db[TASKS].update_one(
            {"_id": task_id},
            {"$set": {"next_run_at": next_run_at, "due_at": due_at, "updated_at": now}},
        )

    # -- the atomic state machines --------------------------------------------

    async def claim_due(
        self, *, task_id: str, seen_due_at: str, next_run_at: str | None,
        due_at: str | None, run: TaskRunRecord, owner: str, lease_until: str, now: str,
    ) -> TaskRunRecord | None:
        """Take the lease on one occurrence and open its run row.

        SQLite does both inside one transaction. Here the run row goes FIRST and
        the claim second, which gets the same guarantee out of two single-document
        operations:

        * The run insert is the occurrence lock. `(task_id, scheduled_at)` is
          unique, so a second caller for the same occurrence is refused here,
          before anything has been mutated — the belt-and-braces case in SQLite
          becomes the first line of defence.
        * The claim is the compare-and-swap, with `due_at = seen_due_at` as the
          token, exactly as in SQLite.

        Doing it the other way round would leave the loser of a race having
        advanced somebody else's schedule. In this order the only thing to undo
        is a run row we minted ourselves, under an id nobody else can hold.
        """
        db = await self._ready()
        try:
            await db[RUNS].insert_one(to_doc(run))
        except DuplicateKeyError:
            return None  # this occurrence already has a run
        claimed = await db[TASKS].find_one_and_update(
            {"_id": task_id, "state": STATE_IDLE, "due_at": seen_due_at},
            {
                "$set": {
                    "state": STATE_RUNNING, "claim_owner": owner, "claim_at": now,
                    "lease_until": lease_until, "next_run_at": next_run_at,
                    "due_at": due_at, "last_run_at": now, "updated_at": now,
                },
                "$inc": {"run_count": 1},
            },
        )
        if claimed is None:
            await db[RUNS].delete_one({"run_id": run.run_id})
            return None
        return run

    async def record_skip(
        self, *, task_id: str, seen_due_at: str, run: TaskRunRecord,
        next_run_at: str | None, due_at: str | None, now: str,
    ) -> bool:
        """An occurrence that will not run. Same ordering as ``claim_due`` and
        for the same reason; no lease, because nothing is going to run, and the
        task's own state is left alone so an overlap skip cannot un-claim the
        run in flight."""
        db = await self._ready()
        try:
            await db[RUNS].insert_one(to_doc(run))
        except DuplicateKeyError:
            return False
        advanced = await db[TASKS].find_one_and_update(
            {"_id": task_id, "due_at": seen_due_at},
            {
                "$set": {
                    "next_run_at": next_run_at, "due_at": due_at,
                    "last_status": run.status, "updated_at": now,
                },
                "$inc": {"miss_count": 1},
            },
        )
        if advanced is None:
            await db[RUNS].delete_one({"run_id": run.run_id})
            return False
        return True

    async def complete_run(
        self, *, run_id: str, task_id: str, status: str, finished_at: str,
        duration_ms: int, detail: str = "", reply_chars: int = 0,
        tool_calls: int = 0, failed: bool = False, release_state: str = STATE_IDLE,
    ) -> None:
        db = await self._ready()
        # `status = running` guards against overwriting a verdict already
        # written by a reclaim: a run that came back after being declared
        # interrupted must not un-declare itself.
        closed = await db[RUNS].update_one(
            {"run_id": run_id, "status": RUN_RUNNING},
            {"$set": {
                "status": status, "finished_at": finished_at,
                "duration_ms": duration_ms, "detail": detail,
                "reply_chars": reply_chars, "tool_calls": tool_calls,
            }},
        )
        if closed.matched_count != 1:
            return
        streak: dict = {"$inc": {"consecutive_failures": 1}} if failed else {}
        update: dict = {"$set": {
            "state": release_state, "claim_owner": "", "lease_until": "",
            "last_status": status, "updated_at": finished_at,
        }}
        if failed:
            update.update(streak)
        else:
            update["$set"]["consecutive_failures"] = 0
        await db[TASKS].update_one({"_id": task_id}, update)

    async def reclaim_orphans(
        self, *, scheduler_id: str, now: str
    ) -> list[TaskRunRecord]:
        db = await self._ready()
        running = [
            from_doc(TaskRunRecord, doc)
            async for doc in db[RUNS].find({"status": RUN_RUNNING})
        ]
        leases = {
            doc["_id"]: doc.get("lease_until", "")
            async for doc in db[TASKS].find(
                {"_id": {"$in": [r.task_id for r in running]}}, {"lease_until": 1}
            )
        }
        orphans = [
            run for run in running
            if run.owner != scheduler_id or _lease_expired(leases, run, now)
        ]
        for orphan in orphans:
            await db[RUNS].update_one(
                {"run_id": orphan.run_id},
                {"$set": {
                    "status": RUN_INTERRUPTED, "finished_at": now,
                    "detail": "the engine stopped while this run was in flight",
                }},
            )
            await db[TASKS].update_one(
                {"_id": orphan.task_id, "state": STATE_RUNNING},
                {
                    "$set": {
                        "state": STATE_IDLE, "claim_owner": "", "lease_until": "",
                        "last_status": RUN_INTERRUPTED, "updated_at": now,
                    },
                    "$inc": {"consecutive_failures": 1},
                },
            )
        return orphans

    async def mark_notified(self, run_id: str) -> None:
        db = await self._ready()
        await db[RUNS].update_one({"run_id": run_id}, {"$set": {"notified": 1}})

    async def prune_runs(self, *, keep_per_task: int, before: str) -> int:
        """Drop finished history older than ``before``, keeping the newest
        ``keep_per_task`` of every task whatever their age.

        SQLite says this in one statement with a window function. Here the
        "newest N per task" set is worked out first and then excluded, which is
        the same rule spelled out rather than a different one: the keep set is
        computed over ALL of a task's runs, not only the ones being considered
        for deletion.
        """
        db = await self._ready()
        keep: list[str] = []
        for task_id in await db[RUNS].distinct("task_id"):
            cursor = (
                db[RUNS].find({"task_id": task_id}, {"run_id": 1})
                .sort([("scheduled_at", -1)]).limit(keep_per_task)
            )
            keep.extend([doc["run_id"] async for doc in cursor])
        result = await db[RUNS].delete_many({
            "finished_at": {"$ne": "", "$lt": before},
            "run_id": {"$nin": keep},
        })
        return result.deleted_count

    # -- the scheduler's own state --------------------------------------------

    async def write_heartbeat(self, beat: SchedulerHeartbeat) -> None:
        db = await self._ready()
        await db[HEARTBEAT].replace_one(
            {"_id": HEARTBEAT_ID}, to_doc(beat, _id=HEARTBEAT_ID), upsert=True
        )

    async def read_heartbeat(self) -> SchedulerHeartbeat | None:
        db = await self._ready()
        doc = await db[HEARTBEAT].find_one({"_id": HEARTBEAT_ID})
        return from_doc(SchedulerHeartbeat, doc) if doc else None


def _lease_expired(
    leases: dict[str, str], run: TaskRunRecord, now: str
) -> bool:
    """Whether the task behind this run has let its lease run out. A live run of
    OUR scheduler keeps its lease fresh by definition of the deadline being
    shorter, so this only catches genuinely abandoned work."""
    if run.task_id not in leases:
        return True  # the task is gone; its run cannot finish
    lease = leases[run.task_id]
    return bool(lease) and lease <= now
