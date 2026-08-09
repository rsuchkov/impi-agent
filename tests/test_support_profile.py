"""The bundled support agent: its profile has to stay true to the engine.

Support ships inside the package, so nobody notices when the engine grows a tool
or a doc page and its profile doesn't. These are the invariants that made that
drift visible the hard way — a tool the docs promised was absent from the
allowlist, so the tool server answered every call with 403.
"""

from pathlib import Path

import yaml

from crucible.profiles import FsProfileStore
from crucible.tools import build_registry
from impi.app import BUILTIN_AGENTS_PATH

SUPPORT = BUILTIN_AGENTS_PATH / "agents" / "support"

# pi's own tools, enabled only by being named in the allowlist, plus the one
# tool the engine's pi extension adds (a blocking yes/no over pi's UI channel).
PI_BUILTINS = frozenset({"read", "write", "edit", "bash", "grep", "find", "ls"})
EXTENSION_TOOLS = frozenset({"ask_user_confirm"})


def _spec():
    return FsProfileStore(str(BUILTIN_AGENTS_PATH)).get("support")


def test_every_tool_support_asks_for_actually_exists() -> None:
    known = PI_BUILTINS | EXTENSION_TOOLS | set(build_registry().names())

    unknown = sorted(set(_spec().tools) - known)

    assert unknown == []  # a typo here is silently a tool the agent never gets


def test_support_has_the_skill_library_tools_the_docs_promise() -> None:
    # docs/skills.md tells the operator to ask support to install and assign
    # skills; these four refuse every other caller, so this list is the only
    # thing that makes that true.
    assert {"list_skills", "install_skill", "assign_skill", "remove_skill"} <= set(
        _spec().tools
    )


def test_every_bundled_skill_is_on_disk_and_declares_itself() -> None:
    for name in _spec().skills:
        skill = SUPPORT / ".pi" / "skills" / name / "SKILL.md"
        assert skill.is_file(), f"{name}: no SKILL.md"

        front = _front_matter(skill)
        # The directory name is the identity; a disagreeing `name:` would make
        # the skill answer to two.
        assert front.get("name") == name
        assert front.get("description"), f"{name}: no description to trigger on"


def _front_matter(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    assert text.startswith("---\n"), f"{path}: no front matter"
    block = text.split("---\n", 2)[1]
    return yaml.safe_load(block) or {}
