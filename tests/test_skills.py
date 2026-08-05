"""The skill library: reading SKILL.md, installing from a directory or a git
repository, and assigning a skill to an agent."""

import subprocess
from pathlib import Path

import pytest

from crucible.profiles import FsProfileStore, ProfileError
from crucible.skills import (
    SkillError,
    SkillLibrary,
    assign_skill,
    assigned_skills,
    declared_tools,
    install,
    parse_source,
    stage,
    unassign_skill,
)

# The front matter Claude / Hermes / ClawHub all write — the reason a skill from
# any of them can be installed here unchanged.
SKILL_MD = """---
name: greek-tutor
description: Teaches Greek vocabulary with spaced repetition
version: 1.2.0
requires_tools: [read, bash]
tags: [language, tutoring]
category: education
metadata:
  hermes:
    config: {}
---

# Greek tutor

Run `scripts/drill.sh` to start a session.
"""


def _write_skill(path: Path, *, body: str = SKILL_MD, script: bool = True) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    (path / "SKILL.md").write_text(body, encoding="utf-8")
    if script:
        scripts = path / "scripts"
        scripts.mkdir(exist_ok=True)
        drill = scripts / "drill.sh"
        drill.write_text("#!/bin/sh\necho drill\n", encoding="utf-8")
        drill.chmod(0o755)
    return path


# --- reading -------------------------------------------------------------------


def test_library_reads_front_matter(tmp_path: Path) -> None:
    _write_skill(tmp_path / "greek-tutor")
    skill = SkillLibrary(tmp_path).get("greek-tutor")

    assert skill.description.startswith("Teaches Greek")
    assert skill.version == "1.2.0"
    assert skill.requires_tools == ("read", "bash")
    assert skill.tags == ("language", "tutoring") and skill.category == "education"
    assert skill.extra["metadata"] == {"hermes": {"config": {}}}  # foreign keys kept


def test_library_lists_and_skips_broken_skills(tmp_path: Path) -> None:
    _write_skill(tmp_path / "greek-tutor")
    _write_skill(tmp_path / "no-front-matter", body="# just markdown\n", script=False)
    (tmp_path / "not-a-skill").mkdir()  # no SKILL.md at all

    names = [s.name for s in SkillLibrary(tmp_path).list()]

    assert names == ["greek-tutor"]  # one bad skill must not hide the library


def test_missing_skill_says_where_it_looked(tmp_path: Path) -> None:
    with pytest.raises(SkillError, match="no skill 'nope'"):
        SkillLibrary(tmp_path).get("nope")


def test_front_matter_without_description_is_refused(tmp_path: Path) -> None:
    _write_skill(tmp_path / "x", body="---\nname: x\n---\nbody\n", script=False)
    with pytest.raises(SkillError, match="no 'description'"):
        SkillLibrary(tmp_path).get("x")


# --- sources -------------------------------------------------------------------


def test_parse_source_forms(tmp_path: Path) -> None:
    local = _write_skill(tmp_path / "local-skill")
    assert parse_source(str(local)).kind == "local"

    gh = parse_source("anthropics/skills/document-skills/pdf@v2")
    assert gh.kind == "git"
    assert gh.location == "https://github.com/anthropics/skills"
    assert (gh.path, gh.ref) == ("document-skills/pdf", "v2")

    url = parse_source("https://example.com/team/skills.git")
    assert url.kind == "git" and url.ref == ""
    # An scp-style URL's @ belongs to the host, not to a ref.
    assert parse_source("git@github.com:team/skills.git").ref == ""


def test_parse_source_rejects_nonsense() -> None:
    with pytest.raises(SkillError, match="unrecognized source"):
        parse_source("this is not a source")


def test_install_from_a_local_directory(tmp_path: Path) -> None:
    source = _write_skill(tmp_path / "src" / "greek-tutor")
    library = SkillLibrary(tmp_path / "library")

    with stage(str(source)) as staged:
        files = dict((name, executable) for name, _size, executable in staged.files())
        assert files["SKILL.md"] is False
        assert files["scripts/drill.sh"] is True  # the trust prompt shows this
        skill = install(library, staged)

    assert skill.name == "greek-tutor"
    assert (library.root / "greek-tutor" / "scripts" / "drill.sh").exists()
    assert library.get("greek-tutor").source is not None


def test_installing_twice_needs_force(tmp_path: Path) -> None:
    source = _write_skill(tmp_path / "src" / "greek-tutor")
    library = SkillLibrary(tmp_path / "library")
    with stage(str(source)) as staged:
        install(library, staged)
        with pytest.raises(SkillError, match="already installed"):
            install(library, staged)
        install(library, staged, force=True)  # what update does


# --- git -----------------------------------------------------------------------


def _git_repo(root: Path) -> str:
    """A real local repository, so the git path is exercised without a network."""
    root.mkdir(parents=True, exist_ok=True)
    _write_skill(root / "skills" / "greek-tutor")
    env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@e", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@e", "PATH": "/usr/bin:/bin"}
    for cmd in (["git", "init", "-q", "-b", "main"], ["git", "add", "-A"],
                ["git", "commit", "-qm", "skill"]):
        subprocess.run(cmd, cwd=root, env=env, check=True, capture_output=True)
    return f"file://{root}"


def test_install_from_a_git_repository_pins_the_commit(tmp_path: Path) -> None:
    url = _git_repo(tmp_path / "repo")
    library = SkillLibrary(tmp_path / "library")

    with stage(f"{url}#skills/greek-tutor@main") as staged:
        skill = install(library, staged)

    assert skill.source is not None
    assert skill.source.kind == "git" and len(skill.source.sha) == 40
    assert skill.source.describe().endswith(f"@{skill.source.sha[:7]}")


def test_git_source_without_the_skill_says_so(tmp_path: Path) -> None:
    url = _git_repo(tmp_path / "repo")
    with pytest.raises(SkillError, match="no such directory"):
        stage(f"{url}#skills/nope@main")


# --- assignment ----------------------------------------------------------------

AGENT_YAML = """\
name: greek-teacher              # MUST equal the directory name
display_name: Greek Teacher
role: language-tutor
description: Greek tutor

runtime:
  provider: openai-codex         # the comment that must survive an edit
  timeout: 180
  tools:
    - read
    - bash
  skills:
    - vocabulary-trainer         # the agent's own skill
"""


def _agent(tmp_path: Path, body: str = AGENT_YAML) -> Path:
    manifest = tmp_path / "agents" / "greek-teacher" / "agent.yaml"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(body, encoding="utf-8")
    return manifest


def test_assign_keeps_the_profile_a_human_wrote(tmp_path: Path) -> None:
    manifest = _agent(tmp_path)

    assert assign_skill(manifest, "greek-tutor") is True

    text = manifest.read_text(encoding="utf-8")
    assert "registry:greek-tutor" in text
    assert "vocabulary-trainer" in text  # the private skill stays
    assert "# the comment that must survive an edit" in text
    assert "# MUST equal the directory name" in text
    assert text.index("display_name") < text.index("runtime")  # key order intact


def test_assign_is_idempotent_and_unassign_removes(tmp_path: Path) -> None:
    manifest = _agent(tmp_path)

    assert assign_skill(manifest, "greek-tutor") is True
    assert assign_skill(manifest, "greek-tutor") is False  # already there
    assert assigned_skills(manifest) == ("greek-tutor",)
    assert declared_tools(manifest) == ("read", "bash")

    assert unassign_skill(manifest, "greek-tutor") is True
    assert unassign_skill(manifest, "greek-tutor") is False
    assert assigned_skills(manifest) == ()
    assert "vocabulary-trainer" in manifest.read_text(encoding="utf-8")


def test_assign_creates_the_block_when_a_profile_has_none(tmp_path: Path) -> None:
    manifest = _agent(tmp_path, "name: greek-teacher\nrole: tutor\n")
    assert assign_skill(manifest, "greek-tutor") is True
    assert assigned_skills(manifest) == ("greek-tutor",)


# --- resolution ----------------------------------------------------------------


def test_profile_resolves_a_library_skill_to_its_directory(tmp_path: Path) -> None:
    library = SkillLibrary(tmp_path / "library")
    _write_skill(library.root / "greek-tutor")
    manifest = _agent(tmp_path)
    assign_skill(manifest, "greek-tutor")

    store = FsProfileStore(tmp_path, library=library.path_if_present)
    spec = store.get("greek-teacher")

    assert str(library.root / "greek-tutor") in spec.skills
    assert "vocabulary-trainer" in spec.skills  # bare names still pass through


def test_profile_refuses_an_uninstalled_library_skill(tmp_path: Path) -> None:
    manifest = _agent(tmp_path)
    assign_skill(manifest, "ghost")

    with pytest.raises(ProfileError, match="unknown library skill 'ghost'"):
        FsProfileStore(tmp_path, library=SkillLibrary(tmp_path / "library").path_if_present)


# --- the support agent's tools ---------------------------------------------------


def _tool_ctx(tmp_path: Path, agent: str = "support"):
    from crucible.tools.base import ToolContext
    from impi.skill_tools import SkillSettings

    class _Directory:
        def agent_user_ids(self) -> dict:
            return {}

    settings = SkillSettings(
        _env_file=None,  # hermetic: never read the developer's real .env  # pyright: ignore[reportCallIssue]
        skills_path=str(tmp_path / "library"),
        agents_path=str(tmp_path),
    )
    return ToolContext(agent_name=agent, directory=_Directory(), settings=settings)  # type: ignore[arg-type]


async def test_tools_are_restricted_to_the_support_agent(tmp_path: Path) -> None:
    from crucible.tools.base import ToolError
    from impi.skill_tools import ListSkills

    with pytest.raises(ToolError, match="restricted to the support agent"):
        await ListSkills().execute(_tool_ctx(tmp_path, agent="assistant"), {})


async def test_list_skills_reports_who_uses_what(tmp_path: Path) -> None:
    from impi.skill_tools import ListSkills

    _write_skill(tmp_path / "library" / "greek-tutor")
    manifest = _agent(tmp_path)
    assign_skill(manifest, "greek-tutor")

    result = await ListSkills().execute(_tool_ctx(tmp_path), {})

    assert [s["name"] for s in result["skills"]] == ["greek-tutor"]
    assert result["skills"][0]["assigned_to"] == ["greek-teacher"]
    assert result["skills"][0]["requires_tools"] == ["read", "bash"]


async def test_install_skill_reports_the_files_it_copied(tmp_path: Path) -> None:
    from impi.skill_tools import InstallSkill

    source = _write_skill(tmp_path / "src" / "greek-tutor")

    result = await InstallSkill().execute(_tool_ctx(tmp_path), {"source": str(source)})

    assert result["installed"] == "greek-tutor"
    # The operator must be able to see what will run inside the engine.
    assert {"path": "scripts/drill.sh", "bytes": 21, "executable": True} in result["files"]


async def test_assign_skill_warns_when_the_agent_lacks_the_tools(tmp_path: Path) -> None:
    from impi.skill_tools import AssignSkill

    _write_skill(tmp_path / "library" / "greek-tutor")
    _agent(tmp_path, AGENT_YAML.replace("    - read\n    - bash\n", "    - read\n"))

    result = await AssignSkill().execute(
        _tool_ctx(tmp_path), {"skill": "greek-tutor", "agent": "greek-teacher"}
    )

    assert result["assigned"] is True and result["changed"] is True
    assert "bash" in result["warning"]


async def test_remove_skill_refuses_while_assigned(tmp_path: Path) -> None:
    from crucible.tools.base import ToolError
    from impi.skill_tools import RemoveSkill

    _write_skill(tmp_path / "library" / "greek-tutor")
    assign_skill(_agent(tmp_path), "greek-tutor")

    with pytest.raises(ToolError, match="still assigned to: greek-teacher"):
        await RemoveSkill().execute(_tool_ctx(tmp_path), {"name": "greek-tutor"})


def test_the_directory_is_the_skill_s_identity(tmp_path: Path) -> None:
    # Installed under another name (or simply renamed): the reference an agent
    # holds is the directory, so that must be the name the library answers to.
    _write_skill(tmp_path / "renamed", body=SKILL_MD, script=False)
    skill = SkillLibrary(tmp_path).get("renamed")
    assert skill.name == "renamed"  # not the "greek-tutor" its front matter claims
