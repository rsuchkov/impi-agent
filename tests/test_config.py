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
