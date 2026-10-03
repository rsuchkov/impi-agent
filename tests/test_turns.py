"""TurnScope and TurnRegistry: what belongs to one turn, bound for its length."""

import pickle

import pytest

from crucible.ports.turn import TurnScope, TurnSecrets
from crucible.turns import TurnRegistry


def test_secrets_name_what_they_hold_and_never_say_it() -> None:
    secrets = TurnSecrets({"cookie": "JSESSIONID=abc123", "token": "t0ps3cret"})
    assert secrets.get("cookie") == "JSESSIONID=abc123"
    assert "cookie" in secrets and secrets.get("missing") is None
    assert secrets.names() == frozenset({"cookie", "token"})
    shown = f"{secrets!r} {secrets}"
    assert "abc123" not in shown and "t0ps3cret" not in shown
    assert "cookie" in shown  # the names are fine; a reader may know what was there
    with pytest.raises(TypeError):
        pickle.dumps(secrets)


def test_a_scope_shows_no_secret_either() -> None:
    scope = TurnScope("t1", TurnSecrets({"cookie": "abc123"}), {"request_id": "r-9"})
    assert "abc123" not in repr(scope)
    assert "r-9" in repr(scope)
    scope.flag("session_expired")
    assert scope.flags == {"session_expired"}


async def test_the_registry_knows_the_turn_only_while_it_is_bound() -> None:
    registry = TurnRegistry()
    scope = TurnScope("t1")
    assert registry.current("assistant--c1") is None
    async with registry.bind("assistant--c1", scope):
        assert registry.current("assistant--c1") is scope
        assert registry.current("assistant--c2") is None  # another conversation
    assert registry.current("assistant--c1") is None


async def test_binding_over_a_bound_turn_restores_it_afterwards() -> None:
    registry = TurnRegistry()
    first, second = TurnScope("t1"), TurnScope("t2")
    async with registry.bind("s", first):
        async with registry.bind("s", second):
            assert registry.current("s") is second
        assert registry.current("s") is first
    assert registry.current("s") is None


async def test_a_failing_turn_still_unbinds() -> None:
    registry = TurnRegistry()
    with pytest.raises(RuntimeError):
        async with registry.bind("s", TurnScope("t1")):
            raise RuntimeError("the runtime died")
    assert registry.current("s") is None
