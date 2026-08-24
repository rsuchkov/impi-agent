"""Secret policies in SQLite (ward/store.py).

Windows and the ledger are the library's — a second consumer, the tool gate,
turned out to want exactly the same two tables. What is here is the part only a
secret has, and therefore the part the engine must not carry: who may ask, and
on what terms.
"""

from pathlib import Path

from crucible.store.sessions import SqliteSessionStore
from ward.store import SecretPolicyRecord, WardStore
from wardline.wire import APPROVAL_ALWAYS, APPROVAL_NEVER

T0 = "2026-08-11T09:00:00+00:00"
T1 = "2026-08-11T09:15:00+00:00"


def _policy(**over) -> SecretPolicyRecord:
    base = dict(
        name="github-token", approval=APPROVAL_ALWAYS, max_grant_s=3600,
        subjects="assistant", description="release automation",
        created_at=T0, updated_at=T0,
    )
    base.update(over)
    return SecretPolicyRecord(**base)  # type: ignore[arg-type]


def _store(tmp_path: Path) -> WardStore:
    return WardStore(tmp_path / "db.sqlite")


async def test_a_policy_round_trips_with_every_field(tmp_path: Path) -> None:
    store = _store(tmp_path)
    try:
        await store.put_policy(_policy())
        assert await store.get_policy("github-token") == _policy()
        assert await store.list_policies() == [_policy()]
    finally:
        await store.close()


async def test_editing_a_policy_keeps_the_date_it_was_created(tmp_path: Path) -> None:
    store = _store(tmp_path)
    try:
        await store.put_policy(_policy())
        await store.put_policy(_policy(subjects="assistant,builder", updated_at=T1))
        policy = await store.get_policy("github-token")
        assert policy is not None
        assert policy.subjects == "assistant,builder"
        assert policy.updated_at == T1
        assert policy.created_at == T0
    finally:
        await store.close()


async def test_subjects_is_an_allowlist_not_a_free_for_all(tmp_path: Path) -> None:
    policy = _policy(subjects="assistant, builder")
    assert policy.allows("assistant")
    assert policy.allows("builder")  # whitespace around a CSV entry is not identity
    assert not policy.allows("support")
    assert not _policy(subjects="").allows("assistant")


async def test_the_secrets_facet_coexists_with_the_others(tmp_path: Path) -> None:
    """One file, several facets: opening twice must not fight over the schema."""
    store = _store(tmp_path)
    try:
        await store.put_policy(_policy(approval=APPROVAL_NEVER))
    finally:
        await store.close()
    reopened = _store(tmp_path)
    try:
        policy = await reopened.get_policy("github-token")
        assert policy is not None and policy.approval == APPROVAL_NEVER
        assert await reopened.list_agents() == []
    finally:
        await reopened.close()


async def test_the_engine_store_has_no_table_for_these(tmp_path: Path) -> None:
    """The point of the facet living here. A plain library store carries the
    windows and the ledger — every application needs those — and nothing that
    only a broker would ever write."""
    store = SqliteSessionStore(tmp_path / "engine.sqlite")
    try:
        tables = {
            row[0]
            for row in store._conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        assert "approval_grants" in tables and "approval_audit" in tables
        assert "secret_policies" not in tables
    finally:
        await store.close()
