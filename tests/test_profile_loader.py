from pathlib import Path

import pytest

from crucible.profiles import FsProfileStore, ProfileError
from crucible.runtimes.pi import build_pi_profile

GOOD_YAML = """\
name: assistant
display_name: R42 Assistant
role: personal-assistant
description: Personal assistant.
runtime:
  provider: openai-codex
  model: gpt-5.5
  timeout: 120
  tools: [list_agents]
"""


def _write_agent(root: Path, name: str, content: str) -> None:
    agent_dir = root / "agents" / name
    agent_dir.mkdir(parents=True)
    (agent_dir / "agent.yaml").write_text(content, encoding="utf-8")


def test_loads_valid_profile(tmp_path: Path) -> None:
    _write_agent(tmp_path, "assistant", GOOD_YAML)
    store = FsProfileStore(tmp_path, library=None)

    spec = store.get("assistant")
    assert spec.display_name == "R42 Assistant"
    assert spec.role == "personal-assistant"
    assert spec.provider == "openai-codex"
    assert spec.model == "gpt-5.5"
    assert spec.timeout == 120.0
    assert spec.tools == ("list_agents",)
    assert spec.profile_dir == tmp_path / "agents" / "assistant"
    assert [s.name for s in store.list()] == ["assistant"]


def test_build_pi_profile_maps_neutral_spec(tmp_path: Path) -> None:
    _write_agent(tmp_path, "assistant", GOOD_YAML)
    store = FsProfileStore(tmp_path, library=None)

    # The pi runtime maps the neutral spec onto its own profile (not the loader).
    profile = build_pi_profile(store.get("assistant"))
    assert profile.name == "assistant"
    assert profile.config_dir == tmp_path / "agents" / "assistant"
    assert profile.timeout == 120.0
    assert profile.tools == ("list_agents",)
    assert profile.provider == "openai-codex"
    assert profile.model == "gpt-5.5"
    assert profile.skills == ()


def test_parses_and_resolves_skills(tmp_path: Path) -> None:
    yaml_with_skills = GOOD_YAML.replace(
        "tools: [list_agents]",
        "tools: [read, bash]\n  skills: [.pi/skills/hello, /abs/skills/greet]",
    )
    _write_agent(tmp_path, "assistant", yaml_with_skills)
    store = FsProfileStore(tmp_path, library=None)

    spec = store.get("assistant")
    profile_dir = tmp_path / "agents" / "assistant"
    # relative resolved against the profile dir; absolute passes through.
    assert spec.skills == (str((profile_dir / ".pi/skills/hello").resolve()), "/abs/skills/greet")
    assert build_pi_profile(spec).skills == spec.skills


def _yaml_with_skills(value: str) -> str:
    return GOOD_YAML.replace("tools: [list_agents]", f"tools: [read, bash]\n  skills: {value}")


def test_bare_skill_name_passes_through_store_and_runtime_resolves_it(tmp_path: Path) -> None:
    _write_agent(tmp_path, "assistant", _yaml_with_skills("[agent-builder]"))
    spec = FsProfileStore(tmp_path, library=None).get("assistant")
    # A bare name (no separator) is NOT resolved by the neutral store.
    assert spec.skills == ("agent-builder",)
    # The pi runtime resolves it to its own .pi/skills/<name> layout.
    profile_dir = tmp_path / "agents" / "assistant"
    assert build_pi_profile(spec).skills == (
        str((profile_dir / ".pi/skills/agent-builder").resolve()),
    )


def test_skills_override_replaces_agent_yaml_list(tmp_path: Path) -> None:
    _write_agent(tmp_path, "assistant", _yaml_with_skills("[agent-builder, skill-authoring]"))
    store = FsProfileStore(tmp_path, skills_override=lambda name: ("agent-builder",), library=None)
    assert store.get("assistant").skills == ("agent-builder",)


def test_skills_override_empty_disables_all(tmp_path: Path) -> None:
    _write_agent(tmp_path, "assistant", _yaml_with_skills("[agent-builder]"))
    store = FsProfileStore(tmp_path, skills_override=lambda name: (), library=None)  # set-but-empty
    assert store.get("assistant").skills == ()


def test_skills_override_none_keeps_agent_yaml(tmp_path: Path) -> None:
    _write_agent(tmp_path, "assistant", _yaml_with_skills("[agent-builder]"))
    store = FsProfileStore(tmp_path, skills_override=lambda name: None, library=None)  # unset
    assert store.get("assistant").skills == ("agent-builder",)


def test_bad_skills_type_raises(tmp_path: Path) -> None:
    _write_agent(
        tmp_path, "assistant",
        GOOD_YAML.replace("tools: [list_agents]", "tools: []\n  skills: notalist"),
    )
    with pytest.raises(ProfileError, match="runtime.skills"):
        FsProfileStore(tmp_path, library=None)


def test_minimal_profile_gets_defaults(tmp_path: Path) -> None:
    _write_agent(
        tmp_path,
        "minimal",
        "name: minimal\nrole: helper\n",
    )
    store = FsProfileStore(tmp_path, default_timeout=99.0, library=None)

    spec = store.get("minimal")
    assert spec.display_name == "minimal"
    assert spec.provider is None  # no store default -> None (pi decides)
    assert spec.model is None
    assert spec.timeout == 99.0
    assert spec.tools == ()


def test_store_defaults_fill_omitted_provider_model(tmp_path: Path) -> None:
    _write_agent(tmp_path, "minimal", "name: minimal\nrole: helper\n")
    store = FsProfileStore(tmp_path, default_provider="openai-codex", default_model="gpt-5.5", library=None)

    spec = store.get("minimal")
    assert spec.provider == "openai-codex"  # filled from the store default
    assert spec.model == "gpt-5.5"


def test_agent_yaml_overrides_store_defaults(tmp_path: Path) -> None:
    _write_agent(tmp_path, "assistant", GOOD_YAML)  # declares provider/model
    store = FsProfileStore(tmp_path, default_provider="other", default_model="other-model", library=None)

    spec = store.get("assistant")
    assert spec.provider == "openai-codex"  # agent.yaml wins over the default
    assert spec.model == "gpt-5.5"


def test_unknown_agent_raises_with_available_names(tmp_path: Path) -> None:
    _write_agent(tmp_path, "assistant", GOOD_YAML)
    store = FsProfileStore(tmp_path, library=None)

    with pytest.raises(ProfileError, match="assistant"):
        store.get("developer")


def test_name_must_match_directory(tmp_path: Path) -> None:
    _write_agent(tmp_path, "assistant", GOOD_YAML.replace("name: assistant", "name: other"))
    with pytest.raises(ProfileError, match="must match its directory"):
        FsProfileStore(tmp_path, library=None)



def test_invalid_yaml_raises_with_path(tmp_path: Path) -> None:
    _write_agent(tmp_path, "assistant", "name: [unclosed")
    with pytest.raises(ProfileError, match="agent.yaml"):
        FsProfileStore(tmp_path, library=None)


def test_empty_repo_raises(tmp_path: Path) -> None:
    (tmp_path / "agents").mkdir()
    with pytest.raises(ProfileError, match="No agent.yaml"):
        FsProfileStore(tmp_path, library=None)


def test_missing_agents_dir_raises(tmp_path: Path) -> None:
    with pytest.raises(ProfileError, match="agents/"):
        FsProfileStore(tmp_path, library=None)
