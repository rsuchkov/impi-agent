"""The session inventory, asked of every backend (crucible/store/base.py).

Nothing here names an implementation: the `store` fixture hands over whichever
backend the run is exercising, so a second one has to answer these the same way
the first does. What is true only of SQLite — its file, its migrations, its
pragmas — lives in test_sqlite_store.py.
"""

import pytest

from crucible.ports.chat.types import KIND_DM, KIND_THREAD
from crucible.runtimes.pi.spawn import safe_session_id as _safe_session_id
from crucible.store import clock, derive_runtime_session_id
from crucible.store.base import InteractionRecord, Store
from tests.conftest import StoreBackend


async def test_get_or_create_is_idempotent(store: Store) -> None:
    first, created_first = await store.get_or_create("assistant", "ch1", "root1", KIND_THREAD)
    second, created_second = await store.get_or_create("assistant", "ch1", "root1", KIND_THREAD)

    assert created_first is True
    assert created_second is False
    assert first == second
    assert first.runtime_session_id == "assistant--root1"
    assert first.kind == KIND_THREAD
    assert len(await store.list()) == 1


async def test_same_conversation_different_agents_are_distinct(store: Store) -> None:
    a, _ = await store.get_or_create("assistant", "ch1", "root1", KIND_THREAD)
    b, _ = await store.get_or_create("developer", "ch1", "root1", KIND_THREAD)

    assert a.runtime_session_id != b.runtime_session_id
    assert len(await store.list()) == 2
    assert [r.agent for r in await store.list("developer")] == ["developer"]


async def test_records_survive_reopen(stores: StoreBackend) -> None:
    # Restarting the engine must not lose the inventory, whatever holds it.
    store = stores.open()
    await store.get_or_create("assistant", "dmch", "dmch", KIND_DM)
    await store.close()

    reopened = stores.open()
    try:
        records = await reopened.list()
        assert len(records) == 1
        assert records[0].conversation_id == "dmch"
        assert records[0].kind == KIND_DM
    finally:
        await reopened.close()


async def test_touch_updates_last_active(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    # last_active is second-resolution, so a touch in the same second as the
    # create would be indistinguishable from one that never wrote. Move the
    # clock instead of the row: it works the same for every backend.
    monkeypatch.setattr(clock, "now_iso", lambda: "2000-01-01T00:00:00+00:00")
    record, _ = await store.get_or_create("assistant", "ch1", "root1", KIND_THREAD)
    monkeypatch.setattr(clock, "now_iso", lambda: "2026-01-01T00:00:00+00:00")

    await store.touch("assistant", "root1")

    refreshed = (await store.list())[0]
    assert refreshed.last_active == "2026-01-01T00:00:00+00:00"
    assert refreshed.created_at == record.created_at  # and the birthday is left alone


async def test_delete_returns_record_and_removes_row(store: Store) -> None:
    await store.get_or_create("assistant", "ch1", "root1", KIND_THREAD)

    deleted = await store.delete("assistant", "root1")
    assert deleted is not None and deleted.conversation_id == "root1"
    assert await store.delete("assistant", "root1") is None
    assert await store.list() == []


def test_derived_ids_are_already_safe_for_pi() -> None:
    # CONTRACT: store-derived ids must pass pi's sanitizer unchanged, or the
    # DB inventory and pi's on-disk files drift apart in naming.
    for agent, conv in (
        ("assistant", "8psmi44tz0m1oq5g7fmumtdqcr"),  # MM post id
        ("agent-builder", "wjmu5xdznjruxezxj3mhew4usr"),
        ("dev.agent", "root_id-123"),
    ):
        derived = derive_runtime_session_id(agent, conv)
        assert _safe_session_id(derived) == derived


def test_derive_sanitizes_hostile_input() -> None:
    # Non-ASCII input on purpose: exercises the sanitizer alphabet.
    assert derive_runtime_session_id("агент", "тред/1") == "1"
    assert derive_runtime_session_id("///", "///") == "session"


async def test_get_by_runtime_session_reverse_lookup(store: Store) -> None:
    rec, _ = await store.get_or_create("assistant", "ch1", "root1", KIND_THREAD)
    found = await store.get_by_runtime_session(rec.runtime_session_id)
    assert found is not None
    assert found.channel_id == "ch1" and found.conversation_id == "root1"
    assert await store.get_by_runtime_session("nope") is None


async def test_interaction_is_one_shot(store: Store) -> None:
    rec = InteractionRecord(
        interaction_id="i1", token="tok", agent="assistant",
        channel_id="ch1", conversation_id="root1", kind=KIND_THREAD,
        created_at="2026-01-01T00:00:00+00:00",
    )
    await store.create_interaction(rec)
    taken = await store.take_interaction("tok")
    assert taken is not None and taken.conversation_id == "root1"
    # second click with the same token gets nothing (consumed)
    assert await store.take_interaction("tok") is None
    assert await store.take_interaction("unknown") is None


async def test_get_or_create_records_last_user_and_refreshes_it(store: Store) -> None:
    rec, created = await store.get_or_create(
        "assistant", "ch1", "root1", KIND_THREAD, user_id="u-first"
    )
    assert created and rec.last_user_id == "u-first"
    # A later turn by another user in the same conversation refreshes it,
    # so a mid-turn tool addresses THIS turn's user.
    rec2, created2 = await store.get_or_create(
        "assistant", "ch1", "root1", KIND_THREAD, user_id="u-second"
    )
    assert not created2 and rec2.last_user_id == "u-second"


async def test_touch_updates_last_user_when_given(store: Store) -> None:
    await store.get_or_create("assistant", "ch1", "root1", KIND_THREAD, user_id="u1")
    await store.touch("assistant", "root1", user_id="u2")
    rec = await store.get_by_runtime_session(
        derive_runtime_session_id("assistant", "root1")
    )
    assert rec is not None and rec.last_user_id == "u2"
    # touch without a user_id leaves it intact
    await store.touch("assistant", "root1")
    rec2 = await store.get_by_runtime_session(
        derive_runtime_session_id("assistant", "root1")
    )
    assert rec2 is not None and rec2.last_user_id == "u2"
