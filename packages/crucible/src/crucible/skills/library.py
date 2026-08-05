"""SkillLibrary: the shared skills directory an engine reads.

One directory, one skill per subdirectory — the same shape Hermes keeps under
``~/.hermes/skills``. It is the LIBRARY, not the assignment: which agent gets
which skill lives in that agent's profile, so this class only answers "what do I
have" and "where does it live on disk".

The directory may be a git repository (that is the point — skills you installed
are reviewable in a diff), but nothing here requires it.
"""

import logging
from pathlib import Path

import yaml

from crucible.skills.models import SKILL_FILE, SOURCE_FILE, Skill, SkillSource

logger = logging.getLogger(__name__)

_FRONT_MATTER_FENCE = "---"


class SkillError(Exception):
    """A skill could not be read, installed or removed."""


class SkillLibrary:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    def list(self) -> list[Skill]:
        """Every readable skill, by name. A broken one is logged and skipped —
        one bad file must not hide the rest of the library."""
        if not self.root.is_dir():
            return []
        skills: list[Skill] = []
        for entry in sorted(self.root.iterdir()):
            if not (entry / SKILL_FILE).is_file():
                continue
            try:
                skills.append(self.read(entry))
            except SkillError as exc:
                logger.warning("skipping skill %s: %s", entry.name, exc)
        return skills

    def get(self, name: str) -> Skill:
        path = self.root / name
        if not (path / SKILL_FILE).is_file():
            raise SkillError(
                f"no skill {name!r} in {self.root} "
                f"(a skill is a directory holding {SKILL_FILE})"
            )
        return self.read(path)

    def has(self, name: str) -> bool:
        return (self.root / name / SKILL_FILE).is_file()

    def path_of(self, name: str) -> Path:
        """Where a skill lives, without reading it — what a runtime is handed."""
        return self.root / name

    def path_if_present(self, name: str) -> Path | None:
        """The resolver a profile store uses: the skill's directory, or None when
        it isn't installed (so the profile fails loudly instead of handing the
        runtime a path to nothing)."""
        return self.path_of(name).resolve() if self.has(name) else None

    @staticmethod
    def read(path: Path) -> Skill:
        """Parse one skill directory."""
        path = Path(path)
        meta = _front_matter(path / SKILL_FILE)
        # The DIRECTORY is the identity: that is what an agent references and what
        # the runtime is handed. A front-matter name that disagrees (skills get
        # renamed on install) would otherwise make the same skill answer to two
        # names, one of which resolves to nothing.
        name = path.name
        declared = str(meta.pop("name", "") or "").strip()
        if declared and declared != name:
            logger.debug("skill %s: front matter says name=%r — using the directory", name, declared)
        description = str(meta.pop("description", "") or "").strip()
        if not description:
            raise SkillError(f"{path / SKILL_FILE}: front matter has no 'description'")
        requires = meta.pop("requires_tools", ()) or ()
        if isinstance(requires, str):
            requires = [requires]
        tags = meta.pop("tags", ()) or ()
        if isinstance(tags, str):
            tags = [tags]
        return Skill(
            name=name,
            description=description,
            path=path,
            version=str(meta.pop("version", "") or ""),
            requires_tools=tuple(str(t).strip() for t in requires if str(t).strip()),
            tags=tuple(str(t).strip() for t in tags if str(t).strip()),
            category=str(meta.pop("category", "") or ""),
            source=_read_source(path / SOURCE_FILE),
            extra=meta,
        )


def _front_matter(skill_file: Path) -> dict:
    """The YAML block fenced by --- at the top of SKILL.md. A skill without one
    is not a skill: the description is what tells an agent when to reach for it."""
    try:
        text = skill_file.read_text(encoding="utf-8")
    except OSError as exc:
        raise SkillError(f"{skill_file}: cannot read ({exc})") from exc
    lines = text.splitlines()
    if not lines or lines[0].strip() != _FRONT_MATTER_FENCE:
        raise SkillError(f"{skill_file}: no YAML front matter (a --- fenced block on line 1)")
    try:
        end = next(i for i, line in enumerate(lines[1:], start=1) if line.strip() == _FRONT_MATTER_FENCE)
    except StopIteration:
        raise SkillError(f"{skill_file}: front matter is never closed by ---") from None
    try:
        meta = yaml.safe_load("\n".join(lines[1:end]))
    except yaml.YAMLError as exc:
        raise SkillError(f"{skill_file}: invalid front matter: {exc}") from exc
    if meta is None:
        return {}
    if not isinstance(meta, dict):
        raise SkillError(f"{skill_file}: front matter must be a mapping")
    return meta


def _read_source(source_file: Path) -> SkillSource | None:
    if not source_file.is_file():
        return None  # hand-written skill: no provenance to show
    import json

    try:
        data = json.loads(source_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        logger.warning("%s: unreadable provenance, ignoring", source_file)
        return None
    return SkillSource(
        kind=str(data.get("kind", "")),
        location=str(data.get("location", "")),
        path=str(data.get("path", "")),
        ref=str(data.get("ref", "")),
        sha=str(data.get("sha", "")),
        installed_at=str(data.get("installed_at", "")),
    )
