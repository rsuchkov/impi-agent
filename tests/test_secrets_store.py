"""Secret policies in SQLite (crucible/store/secrets.py).

Windows and the ledger moved to the shared approval store when a second thing —
the tool gate — turned out to want exactly the same two tables; what is left
here is the part only a secret has: who may ask, and on what terms.
"""

from pathlib import Path

from crucible.store.base import (
    APPROVAL_ALWAYS,
    APPROVAL_NEVER,
    SecretPolicyRecord,
)
from crucible.store.sessions import SqliteSessionStore

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


def _store(tmp_path: Path) -> SqliteSessionStore:
    return SqliteSessionStore(tmp_path / "db.sqlite")


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
