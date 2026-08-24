"""Where to ask and what to prove, worked out from the environment
(wardline/identity.py).

This is the only configuration either program has, and it is the first thing a
fresh deployment gets wrong — so what these pin is mostly the errors: each one
names the variable that would have answered it.

The derivation itself matters too. An agent's certificate is found by its own
name, which is what lets the engine hand out no secret configuration at all: it
tells an agent who it is, and the deployment says where the identities are.
"""

from pathlib import Path

import pytest

from wardline.identity import (
    IdentityError,
    agent_identity,
    certs_dir,
    operator_identity,
)

URL = "https://ward:8425"


def _env(**over) -> dict[str, str]:
    base = {"SECRET_BROKER_URL": URL, "SECRET_BROKER_CERTS_DIR": "/certs"}
    base.update(over)
    return {k: v for k, v in base.items() if v is not None}


# -- an agent -------------------------------------------------------------------


def test_an_agent_finds_its_identity_by_its_own_name() -> None:
    identity = agent_identity(_env(AGENT_NAME="assistant"))
    assert identity.url == URL
    assert identity.certificate == Path("/certs/assistant.crt")
    assert identity.key == Path("/certs/assistant.key")
    # The authority is the one file every identity shares.
    assert identity.ca == Path("/certs/ca.crt")


def test_the_authority_key_is_never_among_them() -> None:
    """The broker signs; nothing on this side does. If a deployment ever mounted
    the CA key here, no path this module builds would point at it."""
    identity = agent_identity(_env(AGENT_NAME="assistant"))
    assert not any("ca.key" in str(path) for path in
                   (identity.certificate, identity.key, identity.ca))


def test_explicit_paths_win_over_the_derivation() -> None:
    """For a deployment that mounts identities somewhere of its own."""
    identity = agent_identity(
        _env(
            AGENT_NAME="assistant",
            SECRET_BROKER_CERT="/elsewhere/id.pem",
            SECRET_BROKER_KEY="/elsewhere/id.key",
            SECRET_BROKER_CA="/elsewhere/authority.pem",
        )
    )
    assert identity.certificate == Path("/elsewhere/id.pem")
    assert identity.ca == Path("/elsewhere/authority.pem")


def test_a_trailing_slash_on_the_address_is_not_a_second_slash() -> None:
    assert agent_identity(_env(SECRET_BROKER_URL=f"{URL}/", AGENT_NAME="a")).url == URL


def test_without_a_name_or_explicit_paths_it_says_which_would_do() -> None:
    with pytest.raises(IdentityError) as raised:
        agent_identity(_env())
    message = str(raised.value)
    assert "AGENT_NAME" in message and "SECRET_BROKER_CERT" in message


def test_with_no_broker_at_all_it_says_that_first(tmp_path: Path) -> None:
    """The commonest case by far: the deployment simply has no secret store."""
    with pytest.raises(IdentityError) as raised:
        agent_identity({"AGENT_NAME": "assistant"})
    assert "SECRET_BROKER_URL" in str(raised.value)


# -- the operator ------------------------------------------------------------------


def test_the_operator_identity_is_derived_the_same_way(tmp_path: Path) -> None:
    (tmp_path / "operator.crt").write_text("cert")
    (tmp_path / "operator.key").write_text("key")
    identity = operator_identity(_env(SECRET_BROKER_CERTS_DIR=str(tmp_path)))
    assert identity.certificate == tmp_path / "operator.crt"
    assert identity.key == tmp_path / "operator.key"


def test_an_operator_without_the_files_is_told_which_are_missing(tmp_path: Path) -> None:
    """Derived paths are checked on disk, because "no operator certificate" and
    "the wrong certificate" are answered very differently by the broker: the
    second reaches it and comes back a 404."""
    with pytest.raises(IdentityError) as raised:
        operator_identity(_env(SECRET_BROKER_CERTS_DIR=str(tmp_path)))
    assert "operator.crt" in str(raised.value)


def test_an_agent_identity_is_not_an_operator_identity(tmp_path: Path) -> None:
    """Two names, two certificates. The broker's operator routes answer only to
    the one, and nothing here quietly substitutes the other."""
    (tmp_path / "assistant.crt").write_text("cert")
    (tmp_path / "assistant.key").write_text("key")
    with pytest.raises(IdentityError):
        operator_identity(_env(SECRET_BROKER_CERTS_DIR=str(tmp_path), AGENT_NAME="assistant"))


# -- the shared corner -----------------------------------------------------------


def test_the_certificates_directory_is_optional() -> None:
    assert certs_dir({}) is None
    assert certs_dir({"SECRET_BROKER_CERTS_DIR": "/certs"}) == Path("/certs")


def test_a_context_cannot_be_built_from_files_that_are_not_there() -> None:
    """The failure a wrong path produces, as an error an operator can act on
    rather than an OSError from inside ssl."""
    identity = agent_identity(_env(AGENT_NAME="nobody"))
    with pytest.raises(IdentityError) as raised:
        identity.context()
    assert "nobody.crt" in str(raised.value)
