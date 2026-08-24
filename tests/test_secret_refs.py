"""Parsing a secret reference (wardline/wire.py).

A reference becomes a path on the backend, so this is a boundary check as much
as a parser: the tests below are mostly about what must NOT be accepted.
"""

import pytest

from wardline.wire import DEFAULT_FIELD, SecretRef, parse_ref


def test_both_schemes_mean_the_same_thing() -> None:
    assert parse_ref("vault://github-token") == SecretRef("github-token", DEFAULT_FIELD)
    assert parse_ref("secret://github-token") == SecretRef("github-token", DEFAULT_FIELD)


def test_a_field_can_be_named_after_a_hash() -> None:
    assert parse_ref("vault://smtp#password") == SecretRef("smtp", "password")
    assert parse_ref("vault://smtp#username") == SecretRef("smtp", "username")


def test_surrounding_whitespace_is_not_part_of_the_name() -> None:
    assert parse_ref("  vault://npm-token \n") == SecretRef("npm-token", DEFAULT_FIELD)


def test_a_bare_name_is_not_a_reference() -> None:
    # Otherwise `--env FOO=bar` would quietly become a request for a secret
    # called bar, and a typo would read something it shouldn't.
    with pytest.raises(ValueError):
        parse_ref("github-token")
    with pytest.raises(ValueError):
        parse_ref("https://example.test/token")


@pytest.mark.parametrize(
    "raw",
    [
        "vault://../../sys/policies",  # climbing out of the engine's mount
        "vault://a/b",
        "vault://",
        "vault://Github-Token",  # names are lowercase, so they can't be ambiguous
        "vault://tok en",
        "vault://" + "x" * 65,
        "vault://ok#bad field",
        "vault://ok#",
        "vault://-leading-dash",
    ],
)
def test_a_name_that_could_escape_the_mount_is_refused(raw: str) -> None:
    with pytest.raises(ValueError):
        parse_ref(raw)


def test_the_error_never_says_whether_the_secret_exists() -> None:
    """The refusal has to read the same for a name that exists and one that
    doesn't — the parser has no idea either way, and must not imply it does."""
    with pytest.raises(ValueError) as caught:
        parse_ref("vault://Nope")
    message = str(caught.value).lower()
    assert "malformed" in message
    assert "exist" not in message and "unknown" not in message


def test_a_reference_prints_back_as_the_reference_it_came_from() -> None:
    # This string lands in the approval card the human reads, so the round trip
    # matters: they must be approving the reference that was actually asked for.
    assert str(parse_ref("vault://github-token")) == "vault://github-token"
    assert str(parse_ref("vault://smtp#password")) == "vault://smtp#password"
