"""Fetching a skill from where it lives into a staging directory.

Two source kinds in this version: a local directory, and a git repository
(``owner/repo[/path][@ref]`` for GitHub, or any git URL). Fetching is always a
two-step: **stage** puts the skill in a temporary directory so the caller can
show what is about to be installed, and only then does the library copy it in.
That is the whole trust model — a skill carries scripts that will run inside the
engine with the agent's tools, so nothing lands unseen, and a git install is
pinned to the exact commit it was taken from.
"""

import json
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from crucible.skills.library import SkillError, SkillLibrary
from crucible.skills.models import SKILL_FILE, SOURCE_FILE, Skill, SkillSource

_GIT_TIMEOUT = 120.0
# owner/repo[/sub/dir][@ref] — the shorthand Hermes and Claude both use.
_SHORTHAND = re.compile(r"^(?P<owner>[\w.-]+)/(?P<repo>[\w.-]+)(?P<path>(?:/[\w.\-/]+)?)$")
_URL_SCHEMES = ("https://", "http://", "ssh://", "git://", "file://", "git@")


@dataclass
class StagedSkill:
    """A skill fetched but not yet installed: what it is, where it came from, and
    every file that would land in the library."""

    skill: Skill
    source: SkillSource
    root: Path  # the staged skill directory (inside a temp dir)
    _workdir: Path | None = None

    def files(self) -> list[tuple[str, int, bool]]:
        """(relative path, size, is-executable), directories excluded, sorted.
        Executability matters: those are the files that will run."""
        out: list[tuple[str, int, bool]] = []
        for item in sorted(self.root.rglob("*")):
            if item.is_dir() or item.is_symlink():
                continue
            stat = item.stat()
            out.append((
                str(item.relative_to(self.root)),
                stat.st_size,
                bool(stat.st_mode & 0o111),
            ))
        return out

    def close(self) -> None:
        if self._workdir is not None and self._workdir.exists():
            shutil.rmtree(self._workdir, ignore_errors=True)
        self._workdir = None

    def __enter__(self) -> "StagedSkill":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def parse_source(spec: str) -> SkillSource:
    """Read a source specification. Accepts a directory path, ``owner/repo`` (+
    optional subdirectory and ``@ref``) and any git URL."""
    spec = spec.strip()
    if not spec:
        raise SkillError("empty skill source")
    location, ref = _split_ref(spec)
    if location.startswith(_URL_SCHEMES):
        # A full URL has no room for a subdirectory, so it goes in the fragment:
        # https://host/team/skills.git#skills/greek-tutor@main
        location, _, subdir = location.partition("#")
        return SkillSource(kind="git", location=location, path=subdir.strip("/"), ref=ref)
    path = Path(location).expanduser()
    if path.exists():
        if ref:
            raise SkillError(f"{location}: a local directory takes no @ref")
        return SkillSource(kind="local", location=str(path.resolve()))
    match = _SHORTHAND.match(location)
    if not match:
        raise SkillError(
            f"unrecognized source {spec!r} — use a directory path, "
            f"owner/repo[/path][@ref], or a git URL"
        )
    return SkillSource(
        kind="git",
        location=f"https://github.com/{match['owner']}/{match['repo']}",
        path=match["path"].strip("/"),
        ref=ref,
    )


def stage(source: SkillSource | str) -> StagedSkill:
    """Fetch ``source`` into a temporary directory and read the skill it holds.
    The caller must ``close()`` the result (or use it as a context manager)."""
    src = parse_source(source) if isinstance(source, str) else source
    workdir = Path(tempfile.mkdtemp(prefix="impi-skill-"))
    try:
        if src.kind == "local":
            root = Path(src.location)
            resolved = src
        else:
            root, sha = _clone(src, workdir)
            resolved = SkillSource(
                kind="git", location=src.location, path=src.path, ref=src.ref, sha=sha
            )
        if not (root / SKILL_FILE).is_file():
            raise SkillError(
                f"{src.describe()}: no {SKILL_FILE} there — point at the skill's own "
                f"directory (owner/repo/path/to/skill)"
            )
        skill = SkillLibrary.read(root)
        return StagedSkill(skill=skill, source=resolved, root=root, _workdir=workdir)
    except Exception:
        shutil.rmtree(workdir, ignore_errors=True)
        raise


def install(
    library: SkillLibrary, staged: StagedSkill, *, name: str = "", force: bool = False
) -> Skill:
    """Copy a staged skill into the library under ``name`` (default: its own).
    Refuses to overwrite unless ``force`` — an update passes it."""
    target_name = (name or staged.skill.name).strip()
    if not target_name or "/" in target_name or target_name.startswith("."):
        raise SkillError(f"invalid skill name {target_name!r}")
    target = library.root / target_name
    if target.exists() and not force:
        raise SkillError(f"skill {target_name!r} is already installed — use update, or force")
    library.root.mkdir(parents=True, exist_ok=True)
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(staged.root, target)
    _write_source(target, staged.source)
    return library.get(target_name)


def _split_ref(spec: str) -> tuple[str, str]:
    """Split a trailing ``@ref``. Careful with scp-style git URLs
    (``git@github.com:o/r``), where the @ belongs to the host part."""
    head, sep, tail = spec.rpartition("@")
    if not sep or not tail or "/" in tail or ":" in tail:
        return spec, ""
    return head, tail


def _clone(source: SkillSource, workdir: Path) -> tuple[Path, str]:
    checkout = workdir / "repo"
    cmd = ["git", "clone", "--depth", "1", "--quiet"]
    if source.ref:
        cmd += ["--branch", source.ref]
    cmd += [source.location, str(checkout)]
    _git(cmd, what=f"clone {source.location}")
    sha = _git(
        ["git", "-C", str(checkout), "rev-parse", "HEAD"], what="read the commit"
    ).strip()
    root = checkout / source.path if source.path else checkout
    if not root.is_dir():
        raise SkillError(f"{source.describe()}: no such directory in the repository")
    return root, sha


def _git(cmd: list[str], *, what: str) -> str:
    try:
        done = subprocess.run(  # noqa: S603 - fixed argv, no shell
            cmd, capture_output=True, text=True, timeout=_GIT_TIMEOUT, check=False
        )
    except FileNotFoundError as exc:
        raise SkillError("git is not installed — needed to fetch a skill") from exc
    except subprocess.TimeoutExpired as exc:
        raise SkillError(f"could not {what}: git timed out") from exc
    if done.returncode != 0:
        detail = (done.stderr or done.stdout).strip().splitlines()
        raise SkillError(f"could not {what}: {detail[-1] if detail else 'git failed'}")
    return done.stdout


def _write_source(target: Path, source: SkillSource) -> None:
    stamped = {
        "kind": source.kind,
        "location": source.location,
        "path": source.path,
        "ref": source.ref,
        "sha": source.sha,
        "installed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    (target / SOURCE_FILE).write_text(json.dumps(stamped, indent=2) + "\n", encoding="utf-8")
