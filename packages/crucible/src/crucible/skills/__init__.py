"""The skill library: skills an engine can hand to any of its agents.

A skill is a directory with a ``SKILL.md`` — the format Claude, Hermes and
ClawHub share, so skills written for them work here unchanged. The library is
one directory (its own git repository, if you want the code of everything you
installed to be reviewable in a diff); which agent gets which skill is declared
in that agent's profile, not here.
"""

from crucible.skills.assignment import (
    LIBRARY_PREFIX,
    assign_skill,
    assigned_skills,
    declared_tools,
    library_ref,
    unassign_skill,
)
from crucible.skills.library import SkillError, SkillLibrary
from crucible.skills.models import SKILL_FILE, Skill, SkillSource
from crucible.skills.sources import StagedSkill, install, parse_source, stage

__all__ = [
    "LIBRARY_PREFIX",
    "SKILL_FILE",
    "Skill",
    "SkillError",
    "SkillLibrary",
    "SkillSource",
    "StagedSkill",
    "assign_skill",
    "assigned_skills",
    "declared_tools",
    "install",
    "library_ref",
    "parse_source",
    "stage",
    "unassign_skill",
]
