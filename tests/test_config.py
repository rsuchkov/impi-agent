"""Settings.integrations public-url resolution (auto / default / explicit)."""

from crucible.config import Settings, _detect_lan_ip


def _settings(**over) -> Settings:
    # Explicit kwargs override any .env value (init > env in pydantic-settings),
    # so the fields under test are deterministic regardless of the local .env.
    return Settings(integrations_port=8423, **over)


def test_public_url_auto_resolves_to_ip_and_port() -> None:
    url = _settings(integrations_public_url="auto").integrations.public_url
    assert url.startswith("http://") and url.endswith(":8423")
    assert "auto" not in url  # the sentinel was resolved, not passed through


def test_public_url_empty_defaults_to_host_containers_internal() -> None:
    url = _settings(integrations_public_url="").integrations.public_url
    assert url == "http://host.containers.internal:8423"


def test_public_url_explicit_is_used_verbatim() -> None:
    url = _settings(integrations_public_url="http://10.0.0.9:8423").integrations.public_url
    assert url == "http://10.0.0.9:8423"


def test_endpoint_urls_join_safely() -> None:
    ints = _settings(integrations_public_url="http://h:8423").integrations
    assert ints.interact_url == "http://h:8423/interact"
    assert ints.dialog_url == "http://h:8423/dialog"
    # a trailing slash on public_url must not double the separator
    trailing = _settings(integrations_public_url="http://h:8423/").integrations
    assert trailing.interact_url == "http://h:8423/interact"


def test_detect_lan_ip_returns_a_host() -> None:
    ip = _detect_lan_ip()
    assert isinstance(ip, str) and ip  # a real IP, or the fallback host


def _no_dotenv() -> Settings:
    return Settings(dotenv_path="no-such.env")  # isolate from the real .env


def test_skills_for_unset_is_none(monkeypatch) -> None:
    monkeypatch.delenv("AGENTS_SKILLS__SUPPORT", raising=False)
    assert _no_dotenv().skills_for("support") is None  # unset -> keep agent.yaml


def test_skills_for_csv_parses_to_tuple(monkeypatch) -> None:
    monkeypatch.setenv("AGENTS_SKILLS__SUPPORT", "agent-builder, skill-authoring")
    assert _no_dotenv().skills_for("support") == ("agent-builder", "skill-authoring")


def test_skills_for_empty_disables_all(monkeypatch) -> None:
    monkeypatch.setenv("AGENTS_SKILLS__SUPPORT", "")
    assert _no_dotenv().skills_for("support") == ()  # set-but-empty != unset


def test_skills_for_uppercases_name_and_maps_hyphen(monkeypatch) -> None:
    monkeypatch.setenv("AGENTS_SKILLS__MY_AGENT", "a")
    assert _no_dotenv().skills_for("my-agent") == ("a",)


def test_command_tokens_fall_back_to_the_unsuffixed_key_for_the_default_agent(
    monkeypatch,
) -> None:
    # Same rule the bot tokens follow: a single-agent deployment configures the
    # default agent without spelling its name anywhere.
    monkeypatch.setenv("COMMAND_TOKENS", "tok-a, tok-b")
    settings = _settings(agent_name="assistant", dotenv_path="/dev/null")

    assert settings.command_tokens_for("assistant") == ("tok-a", "tok-b")
    assert settings.command_tokens_for("support") == ()  # nobody else inherits it


def test_a_per_agent_command_token_wins_over_the_unsuffixed_one(monkeypatch) -> None:
    monkeypatch.setenv("COMMAND_TOKENS", "shared")
    monkeypatch.setenv("AGENTS_COMMAND_TOKENS__ASSISTANT", "own")
    settings = _settings(agent_name="assistant", dotenv_path="/dev/null")

    assert settings.command_tokens_for("assistant") == ("own",)


def test_the_tool_trace_is_on_by_default_and_kept_two_weeks() -> None:
    settings = _settings()
    assert settings.tool_trace_enabled is True
    assert settings.tool_trace_retention_days == 14


def test_the_human_answer_window_must_close_before_the_call_deadline() -> None:
    # Past the extension's own deadline the model is told the call failed; an
    # answer arriving later would run it anyway, beside the retry.
    import pytest

    from crucible.config import TOOL_CALL_DEADLINE_S

    assert _settings(integrations_ui_timeout=TOOL_CALL_DEADLINE_S - 31).integrations.ui_timeout
    with pytest.raises(ValueError, match="INTEGRATIONS_UI_TIMEOUT"):
        _settings(integrations_ui_timeout=TOOL_CALL_DEADLINE_S - 30)


def test_http_callers_are_read_like_ws_services(monkeypatch) -> None:
    monkeypatch.setenv("HTTP_CALLER_TOKEN__MY_APP", "tok")
    monkeypatch.setenv("HTTP_CALLER_AGENTS__MY_APP", "helper, scribe")
    monkeypatch.setenv("HTTP_CALLER_TOKEN__OPEN", "tok2")
    settings = _settings(dotenv_path="/dev/null")
    assert settings.http_port == 8428  # 8427 is the agent containers' relay port
    callers = settings.http_callers()
    assert callers["my-app"] == ("tok", ("helper", "scribe"))
    assert callers["open"] == ("tok2", None)  # unset allowlist = every http agent
