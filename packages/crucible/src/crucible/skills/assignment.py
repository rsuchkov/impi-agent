"""Assigning a library skill to an agent: editing ``runtime.skills`` in place.

The assignment lives in the agent's own profile — one source of truth, visible
in a git diff, editable by hand. That profile is a file a person wrote and
commented, so the edit is a round-trip (comments, key order and layout survive);
rewriting it from a parsed dict would silently strip all of that.
"""

from io import StringIO
from pathlib import Path

from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError

from crucible.skills.library import SkillError

# How a library skill is referenced from a profile (mirrors profiles.loader).
LIBRARY_PREFIX = "registry:"


def library_ref(name: str) -> str:
    return f"{LIBRARY_PREFIX}{name}"


def assign_skill(manifest: Path, skill_name: str) -> bool:
    """Add ``registry:<skill_name>`` to the agent's ``runtime.skills``. Returns
    False when it was already there (assigning twice is not an error)."""
    return _edit(manifest, skill_name, add=True)


def unassign_skill(manifest: Path, skill_name: str) -> bool:
    """Remove the skill from the agent — the only "disable" there is. False when
    the agent didn't have it."""
    return _edit(manifest, skill_name, add=False)


def assigned_skills(manifest: Path) -> tuple[str, ...]:
    """Library skill names this agent references (its own private skills and raw
    paths are not library skills and are left out)."""
    data = _load(manifest)
    skills = _skills_list(data, create=False)
    return tuple(
        str(s)[len(LIBRARY_PREFIX):]
        for s in (skills or [])
        if str(s).startswith(LIBRARY_PREFIX)
    )


def declared_tools(manifest: Path) -> tuple[str, ...]:
    """The agent's tool allowlist — what a skill's ``requires_tools`` is checked
    against before promising the user it will work."""
    runtime = _load(manifest).get("runtime") or {}
    tools = runtime.get("tools") or []
    return tuple(str(t) for t in tools)


def _edit(manifest: Path, skill_name: str, *, add: bool) -> bool:
    if not skill_name.strip():
        raise SkillError("empty skill name")
    ref = library_ref(skill_name.strip())
    yaml = _yaml()
    data = _load(manifest, yaml=yaml)
    skills = _skills_list(data, create=add)
    present = ref in [str(s) for s in (skills or [])]
    if add and present:
        return False
    if not add:
        if skills is None or not present:
            return False
        skills.remove(ref)
    else:
        assert skills is not None  # created above
        skills.append(ref)
    _dump(manifest, data, yaml=yaml)
    return True


def _skills_list(data: dict, *, create: bool):
    """The ``runtime.skills`` sequence, created on demand when assigning."""
    runtime = data.get("runtime")
    if runtime is None:
        if not create:
            return None
        runtime = data["runtime"] = {}
    if not isinstance(runtime, dict):
        raise SkillError("agent.yaml: 'runtime' must be a mapping")
    skills = runtime.get("skills")
    if skills is None:
        if not create:
            return None
        skills = runtime["skills"] = []
    if not isinstance(skills, list):
        raise SkillError("agent.yaml: 'runtime.skills' must be a list")
    return skills


def _yaml() -> YAML:
    yaml = YAML()  # round-trip mode: comments and layout are preserved
    yaml.preserve_quotes = True
    yaml.indent(mapping=2, sequence=4, offset=2)
    return yaml


def _load(manifest: Path, *, yaml: YAML | None = None) -> dict:
    try:
        data = (yaml or _yaml()).load(manifest.read_text(encoding="utf-8"))
    except (OSError, YAMLError) as exc:
        raise SkillError(f"{manifest}: cannot read the profile ({exc})") from exc
    if not isinstance(data, dict):
        raise SkillError(f"{manifest}: expected a mapping at the top level")
    return data


def _dump(manifest: Path, data: dict, *, yaml: YAML) -> None:
    buffer = StringIO()
    yaml.dump(data, buffer)
    # Write via a temp file in the same directory: a half-written profile would
    # break every agent on the next reload.
    tmp = manifest.with_suffix(manifest.suffix + ".tmp")
    tmp.write_text(buffer.getvalue(), encoding="utf-8")
    tmp.replace(manifest)
