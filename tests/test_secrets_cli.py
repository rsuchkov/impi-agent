"""`impi secret …` (impi/cli.py), offline.

The commands that touch a value need a backend and are exercised through the
Vault adapter's own tests; what is worth pinning here is the operator-facing
half — policies, windows and the ledger — plus the two rules that keep the
surface honest: nothing prints a value, and a duration always names its unit.
"""

from pathlib import Path

import pytest

import impi.cli as cli
from crucible.store.base import KIND_SECRET, ApprovalAudit, ApprovalGrant
from crucible.store.sessions import SqliteSessionStore

T0 = "2026-08-11T09:00:00+00:00"


@pytest.fixture(autouse=True)
def _isolated_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    env_file = tmp_path / ".env"
    env_file.write_text("GATEWAY=mattermost\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DOTENV_PATH", str(env_file))
    monkeypatch.setenv("AGENTS_PATH", str(tmp_path / "profiles"))
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    for var in ("MATTERMOST_URL", "SECRETS_ENABLED", "SECRETS_APPROVERS"):
        monkeypatch.delenv(var, raising=False)
    return env_file


def _store(tmp_path: Path) -> SqliteSessionStore:
    return SqliteSessionStore(tmp_path / "data" / "impi.db")


# -- policies ------------------------------------------------------------------


def test_a_policy_is_created_and_shown(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(
        [
            "secret", "policy", "set", "github-token",
            "--subjects", "assistant, builder",
            "--max-grant", "15m",
            "--description", "release automation",
        ]
    ) == 0
    capsys.readouterr()

    assert cli.main(["secret", "policy", "show", "github-token"]) == 0
    out = capsys.readouterr().out
    assert "approval : always" in out
    assert "window   : 15 min" in out
    assert "assistant,builder" in out  # the spacing was normalized


def test_editing_a_policy_replaces_it_rather_than_adding_one(tmp_path: Path) -> None:
    assert cli.main(["secret", "policy", "set", "npm-token", "--subjects", "a"]) == 0
    assert cli.main(["secret", "policy", "set", "npm-token", "--subjects", "b"]) == 0
    store = _store(tmp_path)
    try:
        policies = store.list_policies_sync()
        assert len(policies) == 1 and policies[0].subjects == "b"
    finally:
        store.close_sync()


def test_a_policy_with_no_subjects_is_allowed_and_says_nobody(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Creating the policy before deciding who gets it is a normal order of
    operations; the secret is simply unreachable until subjects are named."""
    assert cli.main(["secret", "policy", "set", "aws-key"]) == 0
    assert "(nobody)" in capsys.readouterr().out


@pytest.mark.parametrize("bad", ["15", "soon", "1 hour", "5x", "1hh"])
def test_a_duration_must_name_its_unit(
    bad: str, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["secret", "policy", "set", "x", "--max-grant", bad]) == 1
    assert "not a duration" in capsys.readouterr().err


@pytest.mark.parametrize(
    "text, seconds", [("0", 0), ("never", 0), ("90s", 90), ("15m", 900), ("2h", 7200)]
)
def test_durations_that_do_name_their_unit(text: str, seconds: int) -> None:
    assert cli._duration(text) == seconds


def test_never_asking_is_called_out_when_a_policy_chooses_it(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert cli.main(
        ["secret", "policy", "set", "llm-key", "--subjects", "assistant",
         "--approval", "never"]
    ) == 0
    assert "unattended" in capsys.readouterr().out


def test_showing_nothing_is_not_a_crash(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["secret", "policy", "show"]) == 1
    assert "no policies yet" in capsys.readouterr().err


# -- windows -------------------------------------------------------------------


def test_open_windows_are_listed_and_can_be_closed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    store = _store(tmp_path)
    try:
        store.create_grant_sync(
            ApprovalGrant(
                id="gr_1", kind=KIND_SECRET, principal="assistant", scope="github-token",
                granted_by="u1", granted_at=T0, expires_at="2099-01-01T00:00:00+00:00",
            )
        )
    finally:
        store.close_sync()

    assert cli.main(["secret", "grants"]) == 0
    out = capsys.readouterr().out
    assert "gr_1" in out and "assistant -> github-token" in out

    assert cli.main(["secret", "revoke", "gr_1"]) == 0
    assert "asks again" in capsys.readouterr().out
    assert cli.main(["secret", "grants"]) == 0
    assert "no open windows" in capsys.readouterr().out


def test_revoking_something_that_is_not_open_says_so(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert cli.main(["secret", "revoke", "gr_nope"]) == 1
    assert "no open window" in capsys.readouterr().err


# -- the ledger ----------------------------------------------------------------


def test_the_ledger_shows_the_decision_the_command_and_the_reason(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    store = _store(tmp_path)
    try:
        store.record_decision_sync(
            ApprovalAudit(
                id="au_1", at=T0, kind=KIND_SECRET, principal="assistant",
                scope="github-token", reason="push the release",
                detail="gh release create v1.2.0", decision="approved_once",
                approver="u1", grant_id="", request_id="rq_1", duration_ms=1200,
            )
        )
        store.record_decision_sync(
            ApprovalAudit(
                id="au_2", at=T0, kind=KIND_SECRET, principal="builder", scope="aws-key",
                reason="", detail="", decision="no_policy", approver="", grant_id="",
                request_id="rq_2", duration_ms=3,
            )
        )
    finally:
        store.close_sync()

    assert cli.main(["secret", "audit"]) == 0
    out = capsys.readouterr().out
    assert "approved_once" in out and "gh release create v1.2.0" in out
    assert "push the release" in out
    # The refused attempt is in the ledger too — it is the only trace of it.
    assert "no_policy" in out and "builder -> aws-key" in out

    assert cli.main(["secret", "audit", "--agent", "builder"]) == 0
    filtered = capsys.readouterr().out
    assert "no_policy" in filtered and "approved_once" not in filtered


def test_an_empty_ledger_says_so(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["secret", "audit"]) == 0
    assert "nothing requested yet" in capsys.readouterr().out


# -- talking to the engine -----------------------------------------------------


def test_unlock_without_a_running_engine_explains_where_to_run_it(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The material has to reach the engine's memory, so this command only means
    anything against the live process."""
    monkeypatch.setattr(cli, "_unlock_material", lambda _s, prompt=True: cli.UnlockMaterial(
        unseal_key="k", auth_secret="s"
    ))
    # Nothing is listening on the tool-server port in a test environment.
    assert cli.main(["secret", "unlock"]) == 1
    assert "no running engine" in capsys.readouterr().err


def test_init_refuses_while_secrets_are_off(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["secret", "init"]) == 2
    assert "SECRETS_ENABLED" in capsys.readouterr().err


def test_a_field_argument_must_be_a_pair() -> None:
    with pytest.raises(cli.TaskError):
        cli._split_field("justaname")
    assert cli._split_field("password=hunter2") == ("password", "hunter2")
