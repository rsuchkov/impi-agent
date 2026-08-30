"""CompositeProfileStore: merge user + engine profile sources, reject dup names."""

from pathlib import Path

import pytest

from crucible.profiles import CompositeProfileStore, FsProfileStore, ProfileError

YAML = """\
name: {name}
role: helper
runtime:
  model: gpt-5.5
"""


def _store(root: Path, *names: str) -> FsProfileStore:
    for n in names:
        d = root / "agents" / n
        d.mkdir(parents=True)
        (d / "agent.yaml").write_text(YAML.format(name=n), encoding="utf-8")
    return FsProfileStore(root, library=None)


def test_merges_and_gets_across_sources(tmp_path: Path) -> None:
    comp = CompositeProfileStore(
        [_store(tmp_path / "user", "assistant"), _store(tmp_path / "engine", "support")]
    )
    assert sorted(s.name for s in comp.list()) == ["assistant", "support"]
    assert comp.get("support").name == "support"  # from the engine source
    assert comp.get("assistant").name == "assistant"  # from the user source


def test_get_unknown_raises(tmp_path: Path) -> None:
    comp = CompositeProfileStore([_store(tmp_path / "user", "assistant")])
    with pytest.raises(ProfileError, match="Unknown agent"):
        comp.get("nope")


def test_duplicate_name_across_sources_raises(tmp_path: Path) -> None:
    with pytest.raises(ProfileError, match="duplicate agent name"):
        CompositeProfileStore(
            [_store(tmp_path / "user", "support"), _store(tmp_path / "engine", "support")]
        )


def test_reload_rechecks_duplicates(tmp_path: Path) -> None:
    user_root = tmp_path / "user"
    comp = CompositeProfileStore(
        [_store(user_root, "assistant"), _store(tmp_path / "engine", "support")]
    )  # unique at construction
    # The user later adds a 'support' agent -> collision surfaces on reload.
    d = user_root / "agents" / "support"
    d.mkdir(parents=True)
    (d / "agent.yaml").write_text(YAML.format(name="support"), encoding="utf-8")
    with pytest.raises(ProfileError, match="duplicate agent name"):
        comp.reload()
