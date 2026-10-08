"""LocalHost: the runtime as a child of this process.

The arrangement there always was, and still the default. This is the only host
that builds a command line, because the flags name paths on THIS filesystem — a
host elsewhere is handed the request and builds its own.
"""

import fnmatch
import logging
import os
import shutil
from pathlib import Path

from crucible.confinement import ProcessScope, confined_command, landlock_abi
from crucible.private_files import private_dir
from crucible.runtimes.pi.auth_file import (
    auth_file_path,
    executable_entries,
    runtime_agent_dir,
)
from crucible.runtimes.pi.errors import PiHostError
from crucible.runtimes.pi.spawn import SpawnRequest, conversation_memory_dir
from crucible.runtimes.pi.transport import PiTransport, SubprocessTransport

logger = logging.getLogger(__name__)


class LocalHost:
    """Runs the process as a child of this one, in this filesystem."""

    def __init__(self, *, program: str = "pi") -> None:
        self._program = program
        self._abi: int | None = None

    async def open(self, request: SpawnRequest) -> PiTransport:
        # A credential the runtime would execute is not one a login wrote:
        # see runtimes/pi/auth_file.py. Nothing starts until it is gone.
        path = auth_file_path()
        entries = executable_entries(path)
        if entries:
            logger.error(
                "refusing to start the runtime: %s holds a value it would run as a "
                "command (%s) — no login writes one; replace the file with a fresh login",
                path, ", ".join(entries),
            )
            raise PiHostError(f"the runtime's credential file holds a command value ({', '.join(entries)})")
        session_dir = self._session_dir(request)
        args = command_args(request, session_dir=session_dir)
        cwd = str(request.cwd or request.profile_dir)
        env = _environment(request)
        if request.process_scope is not None:
            # The scope's variables name paths on this filesystem: its own
            # TMPDIR and HOME, its files directory. HOME is the process's own
            # now; pi still has to find its account.
            env.update(request.process_scope.env)
            env["PI_CODING_AGENT_DIR"] = str(runtime_agent_dir())
        if request.process_scope is not None and self._can_confine():
            scope = runtime_process_scope(request, session_dir, program=self._program)
            command = confined_command(scope, [self._program, *args])
            return await SubprocessTransport.spawn(command[0], command[1:], cwd=cwd, env=env)
        return await SubprocessTransport.spawn(self._program, args, cwd=cwd, env=env)

    def _can_confine(self) -> bool:
        """Whether this kernel can confine a process — asked once. Without it a
        process the application wanted confined runs as it always did, and the
        log says so the first time."""
        if self._abi is None:
            self._abi = landlock_abi()
            if not self._abi:
                logger.warning(
                    "this kernel has no Landlock: runtime processes run unconfined, "
                    "whatever confinement level their agents ask for"
                )
        return bool(self._abi)

    async def aclose(self) -> None:
        return

    @staticmethod
    def _session_dir(request: SpawnRequest) -> Path | None:
        # A memoryless run keeps nothing, so it is given nowhere to keep it.
        if request.session_dir is None or request.session_id is None:
            return None
        # Per agent (sanitized ids can collide across agents), per conversation
        # (a confined process writes its own memory and no other), and an
        # ABSOLUTE path — a relative --session-dir resolves from the process's
        # own cwd (the profile dir), scattering session files into the agents dir.
        base = request.session_dir.resolve()
        # Conversation memory: the owner's alone, the agent's directory too.
        private_dir(base / request.agent)
        return private_dir(conversation_memory_dir(base, request.agent, request.session_id))


def command_args(request: SpawnRequest, *, session_dir: Path | None) -> list[str]:
    """The command line for one spawn. Pure: the caller owns any directory that
    has to exist first, so the mapping from request to flags stays assertable."""
    # --approve: RPC mode shows no trust prompt and otherwise ignores the
    # project's own resources (system prompt, permission policy). Approving for
    # the run loads them from the agent's profile dir.
    args = ["--mode", "rpc", "--approve"]
    if request.session_id:
        args += ["--session-id", request.session_id]
    else:
        args += ["--no-session"]
    if session_dir is not None:
        args += ["--session-dir", str(session_dir)]
    for ext in request.extensions:
        args += ["-e", ext]
    return args + _profile_args(request)


def interactive_args(request: SpawnRequest, *, session_dir: Path | None) -> list[str]:
    """The same agent on a terminal, for a person: the profile's flags without
    the engine's. No RPC mode, no tool-bridge extension (there is no engine to
    bridge to), and no session id — the runtime keeps and continues its own
    sessions under ``session_dir``. --approve stays: the profile is the
    operator's own, and a trust prompt on every start would only teach them to
    answer yes."""
    args = ["--approve"]
    if session_dir is not None:
        args += ["--session-dir", str(session_dir)]
    return args + _profile_args(request)


def _profile_args(request: SpawnRequest) -> list[str]:
    """What the PROFILE says, however the process is driven. One place, so the
    engine's runtime and a person's terminal cannot drift apart."""
    # Single capability gate: --tools is the allowlist over built-in, extension
    # and typed tools alike. An empty list yields no tools at all; a built-in an
    # agent wants (e.g. read/bash for skills) is just named in its profile.
    args = ["--tools", ",".join(request.tools)]
    # No ambient skill discovery — each agent gets EXACTLY its declared skills
    # (this also closes the ancestor-dir walk-up the agents directory would leak).
    args += ["--no-skills"]
    for skill in request.skills:
        args += ["--skill", skill]
    if request.provider:
        args += ["--provider", request.provider]
    if request.model:
        args += ["--model", request.model]
    # Extra system-prompt text (e.g. the gateway's response-formatting rules).
    if request.append_system_prompt:
        args += ["--append-system-prompt", request.append_system_prompt]
    return args


# What a child process gets from this one without asking: where programs are,
# who it runs as, locale and time zone, how to reach the network, which
# certificates to trust, and the runtime's own knobs. Everything else in the
# engine's environment — every token it holds — stays here unless a name is
# granted.
INHERITED_ALWAYS = (
    "PATH", "HOME", "USER", "LOGNAME", "SHELL", "TMPDIR", "TMP", "TEMP",
    "LANG", "LANGUAGE", "LC_*", "TZ", "TERM", "COLORTERM",
    "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "ALL_PROXY",
    "http_proxy", "https_proxy", "no_proxy", "all_proxy",
    "SSL_CERT_FILE", "SSL_CERT_DIR", "NODE_EXTRA_CA_CERTS", "NODE_TLS_REJECT_UNAUTHORIZED",
    "PI_*",
)


def _environment(request: SpawnRequest) -> dict[str, str]:
    """What the child is started with: the base set and the names the request
    inherits, taken from this process's environment, then what the request
    grants. The whole environment only for a process that is part of the
    engine (``inherit_all``)."""
    granted = {name: str(path) for name, path in request.env_files.items()}
    if request.inherit_all:
        return {**os.environ, **request.env, **granted}
    patterns = (*INHERITED_ALWAYS, *request.inherit)
    inherited = {
        name: value
        for name, value in os.environ.items()
        if any(fnmatch.fnmatchcase(name, pattern) for pattern in patterns)
    }
    return {**inherited, **request.env, **granted}


# What every process of this runtime reads, wherever its agent's files are: the
# system, the device nodes, the process table (another process's private
# entries stay out of reach — the kernel refuses them outside the domain).
SYSTEM_READABLE = ("/usr", "/lib", "/lib64", "/bin", "/sbin", "/etc", "/opt", "/proc", "/dev")
# Device nodes a shell writes to: discarding output, a terminal, randomness.
DEVICE_WRITABLE = ("/dev/null", "/dev/zero", "/dev/full", "/dev/tty", "/dev/ptmx")


def runtime_process_scope(request: SpawnRequest, session_dir: Path | None, *, program: str) -> ProcessScope:
    """The application's scope for this process, plus what the runtime itself
    needs: its program and node, the system, its memory for this session, the
    files it is handed (skills, extensions, the tool manifest) and its account
    — read, with room for the lock it takes beside the credential and nothing
    else creatable there. Not written: a process that could rewrite the
    credential could plant a command every other process would run
    (auth_file.py); the application keeps the login fresh instead
    (auth_refresh.py)."""
    assert request.process_scope is not None
    readable = [Path(p) for p in SYSTEM_READABLE]
    readable += install_roots(program)
    readable += [request.profile_dir, *(Path(s) for s in request.skills)]
    readable += [Path(e) for e in request.extensions]
    readable += list(request.env_files.values())
    # A file the process is pointed at in the environment it was granted is a
    # file it was meant to read — a manifest passed as a plain variable, a
    # certificate bundle. Without this it would fail to open it, quietly.
    readable += [Path(v) for v in request.env.values() if v.startswith("/") and Path(v).is_file()]
    agent_dir = runtime_agent_dir()
    readable.append(agent_dir)
    writable = [session_dir] if session_dir is not None else []
    return ProcessScope(
        readable=(*request.process_scope.readable, *readable),
        writable=(*request.process_scope.writable, *writable),
        writable_files=(*request.process_scope.writable_files, *(Path(p) for p in DEVICE_WRITABLE)),
        mkdir_only=(*request.process_scope.mkdir_only, agent_dir),
        env=dict(request.process_scope.env),
    )


def install_roots(program: str) -> list[Path]:
    """Where the runtime and node live when that is outside the system paths —
    a developer's own install under their home, say. The directory holding the
    package tree the program resolves into, and node's prefix."""
    roots: list[Path] = []
    for name in (program, "node"):
        found = shutil.which(name)
        if not found:
            continue
        real = Path(found).resolve()
        package_root = next((p.parent for p in real.parents if p.name == "node_modules"), real.parent)
        roots += [package_root, real.parent.parent]
    return roots
