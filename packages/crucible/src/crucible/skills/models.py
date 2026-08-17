"""What a skill is, in neutral terms.

A skill is a directory holding a ``SKILL.md`` whose YAML front matter describes
it — the format Claude, Hermes and ClawHub converged on, which is why a skill
written for any of them drops into the library unchanged. Everything except
``name`` and ``description`` is optional; unknown keys are ignored on purpose
(the file may be newer, or carry another tool's metadata).
"""

from dataclasses import dataclass, field
from pathlib import Path

SKILL_FILE = "SKILL.md"
# Written next to SKILL.md when the engine installs a skill: where it came from
# and at which commit, so `update` knows what to re-fetch and a reviewer can see
# what is running.
SOURCE_FILE = ".skill-source.json"


@dataclass(frozen=True)
class SkillSource:
    """Where an installed skill came from. ``kind`` is "local" | "git"."""

    kind: str
    location: str  # a directory path, or the repository URL / owner-repo
    path: str = ""  # subdirectory within the repository ("" = its root)
    ref: str = ""  # branch or tag asked for ("" = the default branch)
    sha: str = ""  # the exact commit installed, "" for a local copy
    installed_at: str = ""

    def describe(self) -> str:
        """One line for a listing: what a person needs to judge the origin."""
        if self.kind != "git":
            return self.location
        where = f"{self.location}/{self.path}" if self.path else self.location
        pin = self.sha[:7] if self.sha else (self.ref or "?")
        return f"{where}@{pin}"


@dataclass(frozen=True)
class Skill:
    """One skill in the library."""

    name: str  # the directory name, which is what an agent references
    description: str
    path: Path
    version: str = ""
    # Tool names the skill needs in the agent's allowlist. Any skill running its
    # own scripts needs read+bash; declaring it lets the engine warn instead of
    # letting the skill fail silently mid-turn.
    requires_tools: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()
    category: str = ""
    source: SkillSource | None = None
    # Front matter keys we don't model, kept so tooling can show them.
    extra: dict = field(default_factory=dict)

    @property
    def skill_file(self) -> Path:
        return self.path / SKILL_FILE
