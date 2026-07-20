from pathlib import Path

from crucible.ports.agent import AgentSpec
from crucible.ports.chat.gateway import AgentIdentity
from impi.registry import RegistryService
from crucible.store.sessions import SqliteSessionStore


def _spec(name: str, role: str = "helper") -> AgentSpec:
    return AgentSpec(
        name=name,
        display_name=name,
        role=role,
        description=f"{name} agent",
        profile_dir=Path("agents") / name,
    )


def _ident(name: str, user_id: str) -> AgentIdentity:
    return AgentIdentity(user_id=user_id, username=f"r42-{name}")


async def test_sync_builds_directory_and_persists(tmp_path: Path) -> None:
    store = SqliteSessionStore(tmp_path / "db.sqlite")
    registry = RegistryService(store)
    try:
        await registry.sync(
            [_spec("assistant"), _spec("developer", role="developer")],
            {"assistant": _ident("assistant", "uid-a"), "developer": _ident("developer", "uid-d")},
        )

        assert registry.agent_user_ids() == frozenset({"uid-a", "uid-d"})
        names = [info.name for info in registry.list_agents()]
        assert names == ["assistant", "developer"]

        persisted = await store.list_agents()
        assert {info.name: info.user_id for info in persisted} == {
            "assistant": "uid-a",
            "developer": "uid-d",
        }
    finally:
        await store.close()


async def test_sync_skips_agents_without_resolved_ids(tmp_path: Path) -> None:
    store = SqliteSessionStore(tmp_path / "db.sqlite")
    registry = RegistryService(store)
    try:
        await registry.sync(
            [_spec("assistant"), _spec("developer")], {"assistant": _ident("assistant", "uid-a")}
        )

        assert registry.agent_user_ids() == frozenset({"uid-a"})
        assert [info.name for info in registry.list_agents()] == ["assistant"]
    finally:
        await store.close()


async def test_resync_updates_user_id(tmp_path: Path) -> None:
    store = SqliteSessionStore(tmp_path / "db.sqlite")
    registry = RegistryService(store)
    try:
        await registry.sync([_spec("assistant")], {"assistant": _ident("assistant", "uid-old")})
        await registry.sync([_spec("assistant")], {"assistant": _ident("assistant", "uid-new")})

        assert registry.agent_user_ids() == frozenset({"uid-new"})
        persisted = await store.list_agents()
        assert persisted[0].user_id == "uid-new"
    finally:
        await store.close()


async def test_mark_processed_dedups_per_agent(tmp_path: Path) -> None:
    store = SqliteSessionStore(tmp_path / "db.sqlite")
    try:
        assert await store.mark_processed("assistant", "p1") is True
        assert await store.mark_processed("assistant", "p1") is False  # replay
        assert await store.mark_processed("developer", "p1") is True  # per agent
    finally:
        await store.close()
