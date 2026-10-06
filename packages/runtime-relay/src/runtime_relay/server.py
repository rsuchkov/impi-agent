"""The host's front door: one WebSocket per session, one runtime process behind it.

Everything this program does is on this page. A connection arrives with a token,
asks for a process, and from then on the socket IS the process's stdin and
stdout. When the process ends, the host says so and why; when the socket ends
first, the process is stopped, because nobody is listening to it any more.
"""

import asyncio
import collections
import hmac
import importlib.util
import json
import logging
import os
import shutil
import sys
import uuid
from pathlib import Path

from aiohttp import WSMsgType, web

from runtime_relay import wire
from runtime_relay.config import RELAY_VARIABLE_PREFIX, HostConfig
from runtime_relay.spawn import (
    Confinement,
    Spawn,
    SpawnRejected,
    command_args,
    conversation_memory_dir,
    migrate_session_layout,
    parse,
)

logger = logging.getLogger(__name__)

# The retained stderr tail: enough to carry the actual cause of a crash back to
# the engine without unbounded growth.
STDERR_TAIL_LINES = 40
STDERR_TAIL_CHARS = 1500
# One event per stdout line, and a single event (a large tool result) can exceed
# asyncio's default 64 KiB reader buffer. Same room the engine gives it locally.
STREAM_LIMIT = wire.MAX_FRAME_BYTES
# How long a stopped process is given to go quietly before it is killed.
TERMINATE_GRACE_S = 5.0
# What a confined process reads wherever its agent's files are: the system,
# the device nodes, the process table (another process's private entries stay
# out of reach — the kernel refuses them outside the domain). The engine's
# local host grants the same (crucible.runtimes.pi.hosts.local).
SYSTEM_READABLE = ("/usr", "/lib", "/lib64", "/bin", "/sbin", "/etc", "/opt", "/proc", "/dev")
# Device nodes a shell writes to: discarding output, a terminal, randomness.
DEVICE_WRITABLE = ("/dev/null", "/dev/zero", "/dev/full", "/dev/tty", "/dev/ptmx")


class RelayServer:
    """Serves one agent. The identity is config, never something a caller says."""

    def __init__(self, config: HostConfig) -> None:
        self._config = config

    def app(self) -> web.Application:
        app = web.Application()
        app.router.add_get(wire.HEALTH_PATH, self._health)
        app.router.add_get(wire.SESSION_PATH, self._session)
        return app

    async def run(self) -> None:
        if self._config.session_dir:
            # Memory kept before each conversation had a directory of its own
            # moves into one, before any process looks for it there.
            moved = migrate_session_layout(self._config.session_dir)
            if moved:
                logger.info("moved %d conversation memories into a directory each", moved)
        _log_confinement(self._config.launcher)
        runner = web.AppRunner(self.app())
        await runner.setup()
        site = web.TCPSite(runner, self._config.host, self._config.port)
        await site.start()
        logger.info(
            "Runtime relay for %s listening on %s:%d (protocol %d)",
            self._config.agent, self._config.host, self._config.port,
            wire.PROTOCOL_VERSION,
        )
        try:
            await asyncio.Event().wait()
        finally:
            await runner.cleanup()

    # -- endpoints ----------------------------------------------------------

    async def _health(self, _request: web.Request) -> web.Response:
        # Deliberately unauthenticated and deliberately dull: it answers whether
        # this host is up, which is what a container healthcheck asks, and says
        # nothing a caller could not already work out from the address.
        return web.json_response(
            {
                wire.KEY_AGENT: self._config.agent,
                wire.KEY_VERSION: wire.PROTOCOL_VERSION,
            }
        )

    async def _session(self, request: web.Request) -> web.StreamResponse:
        if not self._authorized(request):
            logger.warning("Rejected a session request with a bad token")
            raise web.HTTPUnauthorized(text="bad token\n")
        ws = web.WebSocketResponse(max_msg_size=wire.MAX_FRAME_BYTES)
        await ws.prepare(request)
        try:
            spawn = await self._accept(ws)
        except SpawnRejected as exc:
            logger.warning("Refused a spawn: %s", exc)
            await self._refuse(ws, str(exc))
            return ws
        if spawn is None:
            return ws
        await self._serve(ws, spawn)
        return ws

    # -- handshake ----------------------------------------------------------

    def _authorized(self, request: web.Request) -> bool:
        presented = request.headers.get(wire.TOKEN_HEADER, "")
        return hmac.compare_digest(presented, self._config.token)

    async def _accept(self, ws: web.WebSocketResponse) -> Spawn | None:
        """Read the opening frame. None = the caller went away without asking."""
        message = await ws.receive()
        if message.type is not WSMsgType.TEXT:
            return None
        try:
            payload = json.loads(message.data)
        except ValueError as exc:
            raise SpawnRejected("the spawn frame is not JSON") from exc
        return parse(payload, self._config)

    async def _refuse(self, ws: web.WebSocketResponse, reason: str) -> None:
        await ws.send_str(
            json.dumps({wire.KEY_TYPE: wire.MSG_ERROR, wire.KEY_MESSAGE: reason})
        )
        code = (
            wire.CLOSE_VERSION if "protocol" in reason else wire.CLOSE_PROTOCOL
        )
        await ws.close(code=code, message=reason.encode("utf-8"))

    # -- the session --------------------------------------------------------

    async def _serve(self, ws: web.WebSocketResponse, spawn: Spawn) -> None:
        scratch = self._config.work_dir / f"session-{uuid.uuid4().hex}"
        try:
            process = await self._start(spawn, scratch)
        except OSError as exc:
            logger.exception("Could not start the runtime")
            await self._refuse(ws, f"could not start the runtime: {exc}")
            shutil.rmtree(scratch, ignore_errors=True)
            return
        await ws.send_str(
            json.dumps(
                {wire.KEY_TYPE: wire.MSG_READY, wire.KEY_VERSION: wire.PROTOCOL_VERSION}
            )
        )
        tail: collections.deque[str] = collections.deque(maxlen=STDERR_TAIL_LINES)
        stderr_task = asyncio.ensure_future(self._drain_stderr(process, tail))
        to_client = asyncio.ensure_future(self._pump_stdout(process, ws))
        to_process = asyncio.ensure_future(self._pump_stdin(ws, process))
        try:
            await asyncio.wait(
                [to_client, to_process], return_when=asyncio.FIRST_COMPLETED
            )
            if to_client.done():
                # The process closed its stdout: it is ending. Say why.
                detail = await self._exit_detail(process, stderr_task, tail)
                logger.info("Runtime for %s ended (%s)", self._config.agent, detail)
                await self._say_exit(ws, detail)
            else:
                logger.info("Engine closed the session; stopping the runtime")
        finally:
            for task in (to_client, to_process, stderr_task):
                task.cancel()
            await self._stop(process)
            shutil.rmtree(scratch, ignore_errors=True)
            if not ws.closed:
                await ws.close()

    async def _start(
        self, spawn: Spawn, scratch: Path
    ) -> asyncio.subprocess.Process:
        # The credential file is shared with the engine and every other agent,
        # read-write; a value the runtime would execute (``!…``) is not one a
        # login wrote. Same rule as the engine's (crucible.runtimes.pi.auth_file);
        # kept here because this package does not depend on the library.
        planted = _executable_entries(_auth_file_path())
        if planted:
            raise OSError(
                f"the runtime's credential file holds a command value ({', '.join(planted)}); "
                "not starting — replace it with `impi login`"
            )
        session_dir: Path | None = None
        # A memoryless run keeps nothing, so it is given nowhere to keep it.
        if self._config.session_dir and spawn.session_id:
            root = self._config.session_dir.resolve()
            # Conversation memory: the owner's alone.
            root.mkdir(parents=True, exist_ok=True)
            root.chmod(0o700)
            session_dir = conversation_memory_dir(root, spawn.session_id)
            session_dir.mkdir(mode=0o700, exist_ok=True)
        args = command_args(spawn, config=self._config, session_dir=session_dir)
        env = self._environment(spawn, scratch)
        command = [self._config.runtime_bin, *args]
        if spawn.confinement is not None:
            command = self._confined_command(command, spawn.confinement, env, scratch, session_dir)
        logger.info(
            "Starting the runtime for %s (session %s, %d tool(s), %d skill(s))",
            self._config.agent, spawn.session_id or "-",
            len(spawn.tools), len(spawn.skills),
        )
        # What was ACTUALLY started, for the question this design invites:
        # the engine named things and this side turned them into paths, so
        # "which paths" is the first thing to ask when an agent behaves as if it
        # were missing something. Debug, because it is one long line per turn.
        logger.debug("Runtime command: %s", " ".join(command))
        return await asyncio.create_subprocess_exec(
            *command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(self._config.profile_dir),
            env=env,
            limit=STREAM_LIMIT,
        )

    def _environment(self, spawn: Spawn, scratch: Path) -> dict[str, str]:
        """This container's environment, less this host's own variables, plus
        what the engine granted. The files it shipped are written here and the
        variables point at this side's copy — the path the engine wrote them to
        does not exist over here."""
        own = {name: value for name, value in os.environ.items() if not name.startswith(RELAY_VARIABLE_PREFIX)}
        env = {**own, **spawn.env}
        if spawn.env_files:
            # What the engine shipped for this process alone — the manifest of
            # its tools — in a directory and files only this user can read.
            _private_dir(scratch)
            for name, content in spawn.env_files.items():
                path = scratch / name.lower()
                descriptor = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
                with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                    handle.write(content)
                env[name] = str(path)
        return env

    def _confined_command(
        self,
        command: list[str],
        confinement: Confinement,
        env: dict[str, str],
        scratch: Path,
        session_dir: Path | None,
    ) -> list[str]:
        """``command`` behind the launcher that confines it, and ``env`` pointed
        at what it was given. It reads the system, its program, its profile,
        the skill library, its extensions and the files the engine shipped; it
        writes its own memory, its own scratch ``TMPDIR`` and ``HOME`` and the
        parts of the files directory the engine named; the credential file is
        rewritable in place and nothing else can be created beside it."""
        launcher = self._config.launcher
        if not launcher.is_file():
            raise OSError(f"the confinement launcher {launcher} is missing — the image is out of date")
        tmp, home = _private_dir(_private_dir(scratch) / "tmp"), _private_dir(scratch / "home")
        agent_dir = _auth_file_path().parent
        for directory in confinement.writable:
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        env.update({"TMPDIR": str(tmp), "HOME": str(home), "PI_CODING_AGENT_DIR": str(agent_dir)})
        env.update({name: str(path) for name, path in confinement.variables.items()})
        readable = [Path(p) for p in SYSTEM_READABLE]
        readable += _install_roots(self._config.runtime_bin)
        readable += [Path(sys.prefix), self._config.profile_dir, scratch, agent_dir]
        readable += [extension.parent for extension in self._config.extensions]
        if self._config.library_dir is not None:
            readable.append(self._config.library_dir)
        # A file the process is pointed at in its environment is a file it was
        # meant to read — an identity certificate, a manifest.
        readable += [Path(v) for v in env.values() if v.startswith("/") and Path(v).is_file()]
        writable = [tmp, home, *confinement.writable]
        if session_dir is not None:
            writable.append(session_dir)
        writable_files = [Path(p) for p in DEVICE_WRITABLE]
        writable_files += [agent_dir / "auth.json", agent_dir / "models-store.json"]
        rules: list[str] = []
        for option, paths in (
            ("--readable", readable),
            ("--writable", writable),
            ("--writable-file", writable_files),
            ("--mkdir-only", [agent_dir]),
        ):
            for path in paths:
                rules += [option, str(path)]
        return [sys.executable, "-I", "-S", str(launcher), *rules, "--", *command]

    # -- pumps --------------------------------------------------------------

    async def _pump_stdout(
        self, process: asyncio.subprocess.Process, ws: web.WebSocketResponse
    ) -> None:
        assert process.stdout is not None
        while True:
            raw = await process.stdout.readline()
            if not raw:  # EOF: the process closed stdout / exited
                return
            await ws.send_bytes(raw)

    async def _pump_stdin(
        self, ws: web.WebSocketResponse, process: asyncio.subprocess.Process
    ) -> None:
        assert process.stdin is not None
        async for message in ws:
            if message.type is WSMsgType.BINARY:
                process.stdin.write(message.data)
                await process.stdin.drain()
            elif message.type is WSMsgType.TEXT:
                logger.warning("Ignoring an unexpected control frame mid-session")
            elif message.type is WSMsgType.ERROR:
                logger.warning("Session socket failed: %s", ws.exception())
                return

    async def _drain_stderr(
        self, process: asyncio.subprocess.Process, tail: collections.deque[str]
    ) -> None:
        # stderr must be drained continuously or the pipe buffer fills and the
        # child deadlocks. It goes to this container's log, where `logs <agent>`
        # will find it, and the tail travels back with the exit.
        assert process.stderr is not None
        try:
            while True:
                raw = await process.stderr.readline()
                if not raw:
                    return
                text = raw.decode("utf-8", errors="replace").rstrip()
                if text:
                    logger.warning("runtime stderr: %s", text)
                    tail.append(text)
        except asyncio.CancelledError:
            return

    # -- ending -------------------------------------------------------------

    async def _exit_detail(
        self,
        process: asyncio.subprocess.Process,
        stderr_task: "asyncio.Future[None]",
        tail: collections.deque[str],
    ) -> str:
        try:
            await asyncio.wait_for(process.wait(), timeout=2.0)
        except (asyncio.TimeoutError, ProcessLookupError):
            pass
        try:
            await asyncio.wait_for(asyncio.shield(stderr_task), timeout=0.5)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            pass
        parts = []
        if process.returncode is not None:
            parts.append(f"exit code {process.returncode}")
        joined = "\n".join(tail)[-STDERR_TAIL_CHARS:]
        if joined:
            parts.append(f"last stderr: {joined}")
        return "; ".join(parts)

    async def _say_exit(self, ws: web.WebSocketResponse, detail: str) -> None:
        if ws.closed:
            return
        await ws.send_str(
            json.dumps({wire.KEY_TYPE: wire.MSG_EXIT, wire.KEY_DETAIL: detail})
        )

    async def _stop(self, process: asyncio.subprocess.Process) -> None:
        if process.returncode is not None:
            return
        try:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=TERMINATE_GRACE_S)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
        except ProcessLookupError:
            pass


def _private_dir(path: Path) -> Path:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.chmod(0o700)
    return path


def _install_roots(program: str) -> list[Path]:
    """Where the runtime and node live when that is outside the system paths:
    the package tree the program resolves into, and node's prefix."""
    roots: list[Path] = []
    for name in (program, "node"):
        found = shutil.which(name)
        if not found:
            continue
        real = Path(found).resolve()
        package_root = next((p.parent for p in real.parents if p.name == "node_modules"), real.parent)
        roots += [package_root, real.parent.parent]
    return roots


def _log_confinement(launcher: Path) -> None:
    """One line at startup: whether the processes this host starts can be
    confined. The launcher answers, so the check is the one that will run."""
    if not launcher.is_file():
        logger.warning("confinement: no launcher at %s — a confined spawn will be refused", launcher)
        return
    spec = importlib.util.spec_from_file_location("confinement_launcher", launcher)
    if spec is None or spec.loader is None:
        logger.warning("confinement: %s cannot be loaded", launcher)
        return
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    abi = int(module.landlock_abi())
    if abi:
        logger.info("confinement: Landlock ABI %d", abi)
    else:
        logger.warning("confinement: this kernel has no Landlock — processes run unconfined")


def _auth_file_path() -> Path:
    agent_dir = os.environ.get("PI_CODING_AGENT_DIR") or str(Path.home() / ".pi" / "agent")
    return Path(agent_dir) / "auth.json"


def _executable_entries(path: Path) -> list[str]:
    """Entries of the credential file the runtime would run as a command."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    found: list[str] = []

    def walk(node: object, where: str) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                walk(value, f"{where}.{key}" if where else str(key))
        elif isinstance(node, list):
            for index, value in enumerate(node):
                walk(value, f"{where}[{index}]")
        elif isinstance(node, str) and node.startswith("!"):
            found.append(where)

    walk(data, "")
    return found
