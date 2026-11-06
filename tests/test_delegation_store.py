"""The DelegationStore facet, on every backend the environment reaches: a
task's record is written as it starts, replaced as it ends, read by its id,
and the running ones are what a restart asks for."""

from dataclasses import replace

from crucible.ports.chat.gateway import GATEWAY_SLACK
from crucible.ports.chat.types import KIND_THREAD
from crucible.ports.delegation import DELEGATION_ANSWERED, DELEGATION_RUNNING
from crucible.store.base import DelegationRecord, Store


def _record(task_id: str, **changes) -> DelegationRecord:
    base = DelegationRecord(
        id=task_id, agent="assistant", to="coder", channel_id="ch1", conversation_id="root1", kind=KIND_THREAD,
        gateway=GATEWAY_SLACK, task="Write the migration.", user_id="u-alice", status=DELEGATION_RUNNING,
        started_at="2026-11-06T10:00:00+00:00", files='["spec.md"]', attributes='{"realm": "acme"}', hop_depth=2,
    )
    return replace(base, **changes)


async def test_a_task_is_written_replaced_and_read_by_its_id(store: Store) -> None:
    assert await store.get_delegation("t1") is None
    await store.save_delegation(_record("t1"))
    assert await store.get_delegation("t1") == _record("t1")  # every field, the count as a number
    ended = _record("t1", status=DELEGATION_ANSWERED, finished_at="2026-11-06T10:01:00+00:00",
                    answer="Done.", notice="", delivered_at="2026-11-06T10:01:02+00:00")
    await store.save_delegation(ended)  # a second save of the id replaces
    assert await store.get_delegation("t1") == ended


async def test_the_running_tasks_are_listed_oldest_first(store: Store) -> None:
    await store.save_delegation(_record("later", started_at="2026-11-06T10:05:00+00:00"))
    await store.save_delegation(_record("first"))
    await store.save_delegation(_record("over", status=DELEGATION_ANSWERED))
    assert [r.id for r in await store.running_delegations()] == ["first", "later"]
