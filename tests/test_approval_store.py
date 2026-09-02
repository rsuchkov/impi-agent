"""Windows and the ledger, asked of every backend (crucible/store/base.py).

Shared by everything a human can authorize, so the tests exercise two kinds side
by side: a window of one kind must not answer for a window of another, even when
the agent, the scope and the timing are identical.
"""

from crucible.store.base import (
    KIND_TOOL,
    ApprovalAudit,
    ApprovalGrant,
    Store,
)

# A kind the library does not define — an application brings its own (the secret
# broker's, here), and the column is a plain string precisely so it can.
KIND_SECRET = "secret"

T0 = "2026-08-11T09:00:00+00:00"
T1 = "2026-08-11T09:15:00+00:00"
T2 = "2026-08-11T09:30:00+00:00"


def _grant(**over) -> ApprovalGrant:
    base = dict(
        id="gr_1", kind=KIND_SECRET, principal="assistant", scope="github-token",
        granted_by="u1", granted_at=T0, expires_at=T2, revoked_at="",
    )
    base.update(over)
    return ApprovalGrant(**base)  # type: ignore[arg-type]


def _audit(**over) -> ApprovalAudit:
    base = dict(
        id="au_1", at=T0, kind=KIND_SECRET, principal="assistant", scope="github-token",
        reason="push release", detail="gh release create v1", decision="approved_once",
        approver="u1", grant_id="", request_id="rq_1", duration_ms=1200,
    )
    base.update(over)
    return ApprovalAudit(**base)  # type: ignore[arg-type]


# -- windows -------------------------------------------------------------------


async def test_a_window_is_found_until_it_expires(store: Store) -> None:
    await store.create_grant(_grant())
    found = await store.live_grant(KIND_SECRET, "assistant", "github-token", now=T1)
    assert found == _grant()
    # At the expiry instant the window is over — `>` not `>=`, so a window of
    # zero seconds can never serve a call.
    assert await store.live_grant(KIND_SECRET, "assistant", "github-token", now=T2) is None


async def test_a_window_belongs_to_one_kind_one_principal_and_one_scope(
    store: Store,
) -> None:
    """The reason the key is a triple: 'assistant may use github-token' and
    'assistant may run github-token' would otherwise be the same row."""
    await store.create_grant(_grant())
    assert await store.live_grant(KIND_TOOL, "assistant", "github-token", now=T1) is None
    assert await store.live_grant(KIND_SECRET, "builder", "github-token", now=T1) is None
    assert await store.live_grant(KIND_SECRET, "assistant", "npm-token", now=T1) is None


async def test_two_kinds_coexist_without_seeing_each_other(store: Store) -> None:
    await store.create_grant(_grant(id="gr_secret"))
    await store.create_grant(_grant(id="gr_tool", kind=KIND_TOOL, scope="bash"))
    assert [g.id for g in await store.list_grants(now=T1, kind=KIND_SECRET)] == ["gr_secret"]
    assert [g.id for g in await store.list_grants(now=T1, kind=KIND_TOOL)] == ["gr_tool"]
    assert len(await store.list_grants(now=T1)) == 2


async def test_revoking_closes_the_window_immediately(store: Store) -> None:
    await store.create_grant(_grant())
    assert await store.revoke_grant("gr_1", now=T1) is True
    assert await store.live_grant(KIND_SECRET, "assistant", "github-token", now=T1) is None
    assert await store.revoke_grant("gr_1", now=T1) is False  # changed nothing


async def test_asking_again_extends_rather_than_shortens(store: Store) -> None:
    await store.create_grant(_grant(id="gr_short", expires_at=T1))
    await store.create_grant(_grant(id="gr_long", expires_at=T2))
    live = await store.live_grant(KIND_SECRET, "assistant", "github-token", now=T0)
    assert live is not None and live.id == "gr_long"


async def test_closing_a_whole_scope_is_what_deleting_the_thing_needs(
    store: Store,
) -> None:
    """Deleting a secret has to take its windows with it, or an agent keeps
    reaching something whose permission is gone."""
    await store.create_grant(_grant(id="gr_a", principal="assistant"))
    await store.create_grant(_grant(id="gr_b", principal="builder"))
    await store.create_grant(_grant(id="gr_other", scope="npm-token"))
    assert await store.revoke_scope(KIND_SECRET, "github-token", now=T1) == 2
    assert [g.id for g in await store.list_grants(now=T1)] == ["gr_other"]


async def test_listing_hides_the_dead_ones_unless_asked(store: Store) -> None:
    await store.create_grant(_grant(id="gr_live", expires_at=T2))
    await store.create_grant(_grant(id="gr_gone", expires_at=T1))
    assert [g.id for g in await store.list_grants(now=T1)] == ["gr_live"]
    assert {g.id for g in await store.list_grants(now=T1, include_dead=True)} == {
        "gr_live", "gr_gone",
    }


# -- the ledger ----------------------------------------------------------------


async def test_the_ledger_records_refusals_too(store: Store) -> None:
    await store.record_decision(_audit())
    await store.record_decision(_audit(id="au_2", at=T1, decision="denied"))
    # A name nobody configured: the caller was told nothing, so this row is
    # the only trace that someone went looking.
    await store.record_decision(
        _audit(id="au_3", at=T2, scope="aws-key", decision="no_policy", approver="")
    )
    rows = await store.list_audit()
    assert [r.id for r in rows] == ["au_3", "au_2", "au_1"]  # newest first


async def test_rows_from_one_second_still_come_back_newest_first(store: Store) -> None:
    """The common case, not an edge one: a request and the approval that answers
    it land in the same second, and ids are random tokens — so the tie has to
    break on insertion order or the ledger reads out of sequence."""
    for n in range(5):
        await store.record_decision(_audit(id=f"au_{n}", at=T0))
    assert [r.id for r in await store.list_audit()] == [
        "au_4", "au_3", "au_2", "au_1", "au_0",
    ]
    assert [r.id for r in await store.list_audit(limit=2)] == ["au_4", "au_3"]


async def test_the_rows_of_one_request_are_tied_together(store: Store) -> None:
    """A request for several secrets leaves several rows, and an operator has to
    be able to see which belong to the same click."""
    await store.record_decision(_audit(id="au_1", scope="a", request_id="rq_7"))
    await store.record_decision(_audit(id="au_2", scope="b", request_id="rq_7"))
    await store.record_decision(_audit(id="au_3", scope="c", request_id="rq_8"))
    rows = await store.list_audit()
    assert len([r for r in rows if r.request_id == "rq_7"]) == 2


async def test_the_ledger_filters_by_kind_principal_and_scope(store: Store) -> None:
    await store.record_decision(_audit(id="au_1"))
    await store.record_decision(_audit(id="au_2", scope="npm-token"))
    await store.record_decision(_audit(id="au_3", principal="builder"))
    await store.record_decision(_audit(id="au_4", kind=KIND_TOOL, scope="bash"))
    assert [r.id for r in await store.list_audit(scope="npm-token")] == ["au_2"]
    assert [r.id for r in await store.list_audit(principal="builder")] == ["au_3"]
    assert [r.id for r in await store.list_audit(kind=KIND_TOOL)] == ["au_4"]
    assert await store.list_audit(principal="nobody") == []


def test_the_decision_vocabulary_is_closed_and_complete() -> None:
    """The tuple claims to list every decision the library itself names. Without
    this it is a comment, and the way it rots is somebody adding a constant and
    not the entry — after which "why was it refused" stops being greppable from
    one place. An application that authorizes something of its own extends the
    set on its side and owes itself the same test."""
    from crucible.store import base

    named = {
        value
        for name, value in vars(base).items()
        if name.startswith("DECISION_") and isinstance(value, str)
    }
    assert set(base.DECISIONS) == named
    assert len(base.DECISIONS) == len(named)  # no duplicates either
