"""RemoteHost: a runtime host that is not this process.

The agent's runtime runs in a container of its own; this end opens a WebSocket
per session, asks for a process, and then behaves exactly like the local
transport — lines in, lines out. Everything above (``PiRpcSession``, the pool,
the flows) is unchanged and unaware.

What this module owns, and nothing else does, is the translation between the two
filesystems. The engine knows an agent's skills as absolute paths; the host has
its own copies under its own mounts. Rather than hoping the paths line up, every
one is expressed as a mounted root plus the rest, and a path under no known root
is an error here — at the boundary, with the agent's name in the message —
instead of a runtime that starts without the skill it was promised.
"""

import asyncio
import json
import logging
from collections.abc import AsyncIterator, Mapping
from pathlib import Path

import aiohttp

from crucible.runtimes.pi.errors import PiHostError
from crucible.runtimes.pi.hosts import wire
from crucible.runtimes.pi.spawn import SpawnRequest
from crucible.runtimes.pi.transport import PiTransport

logger = logging.getLogger(__name__)


class RemoteHost:
    """One agent's runtime host, reached over the network.

    A host serves a single agent, so the address and the token are per agent:
    the token is what stops the agent's own shell — which shares the network —
    from asking for a process with an allowlist nobody granted it.
    """

    def __init__(
        self,
        *,
        url: str,
        token: str,
        library_root: Path | None = None,
        connect_timeout: float = 30.0,
        heartbeat: float = 30.0,
    ) -> None:
        self._url = url.rstrip("/")
        self._token = token
        self._library_root = library_root.resolve() if library_root else None
        self._connect_timeout = connect_timeout
        # Pings the host while a turn is in flight, so a container that died
        # mid-answer surfaces as a broken connection instead of a socket that
        # never says anything again.
        self._heartbeat = heartbeat
        self._client: aiohttp.ClientSession | None = None

    async def open(self, request: SpawnRequest) -> PiTransport:
        if request.cwd is not None:
            raise PiHostError(
                f"{request.agent} runs on a host of its own, which has no "
                f"{request.cwd} — a checkout-scoped run needs a local runtime"
            )
        payload = self._spawn_payload(request)
        ws = await self._connect(request.agent)
        try:
            await ws.send_str(json.dumps(payload))
            await self._await_ready(ws, request.agent)
        except BaseException:
            await ws.close()
            raise
        return RemoteTransport(ws)

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.close()
            self._client = None

    # -- internals ----------------------------------------------------------

    def _session(self) -> aiohttp.ClientSession:
        # Built lazily: a ClientSession binds to the running loop, and a host is
        # wired up before there is one. No total timeout — the ceiling on a turn
        # is the runtime's, and a long answer is not a stalled connection.
        if self._client is None or self._client.closed:
            self._client = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(
                    total=None, sock_connect=self._connect_timeout, sock_read=None
                )
            )
        return self._client

    async def _connect(self, agent: str) -> aiohttp.ClientWebSocketResponse:
        try:
            return await self._session().ws_connect(
                f"{self._url}{wire.SESSION_PATH}",
                headers={wire.TOKEN_HEADER: self._token},
                heartbeat=self._heartbeat,
                max_msg_size=wire.MAX_FRAME_BYTES,
            )
        except aiohttp.WSServerHandshakeError as exc:
            raise PiHostError(
                f"{agent}'s runtime host at {self._url} refused the connection "
                f"({exc.status}) — check its token"
            ) from exc
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
            raise PiHostError(
                f"{agent}'s runtime host at {self._url} is not reachable: {exc} — "
                f"its container may not be running"
            ) from exc

    async def _await_ready(
        self, ws: aiohttp.ClientWebSocketResponse, agent: str
    ) -> None:
        try:
            message = await asyncio.wait_for(ws.receive(), self._connect_timeout)
        except asyncio.TimeoutError as exc:
            raise PiHostError(
                f"{agent}'s runtime host did not answer the spawn within "
                f"{self._connect_timeout:.0f}s"
            ) from exc
        if message.type is not aiohttp.WSMsgType.TEXT:
            raise PiHostError(
                f"{agent}'s runtime host closed the connection instead of "
                f"starting a process ({message.type.name.lower()})"
            )
        frame = _decode(message.data, agent)
        kind = frame.get(wire.KEY_TYPE)
        if kind == wire.MSG_READY:
            return
        if kind == wire.MSG_ERROR:
            raise PiHostError(
                f"{agent}'s runtime host refused the spawn: "
                f"{frame.get(wire.KEY_MESSAGE) or 'no reason given'}"
            )
        raise PiHostError(
            f"{agent}'s runtime host answered {kind!r}, which is not part of "
            f"protocol {wire.PROTOCOL_VERSION}"
        )

    def _spawn_payload(self, request: SpawnRequest) -> dict[str, object]:
        return {
            wire.KEY_TYPE: wire.MSG_SPAWN,
            wire.KEY_VERSION: wire.PROTOCOL_VERSION,
            wire.KEY_AGENT: request.agent,
            wire.KEY_SESSION_ID: request.session_id,
            wire.KEY_TOOLS: list(request.tools),
            wire.KEY_SKILLS: [
                self._skill_ref(skill, request) for skill in request.skills
            ],
            wire.KEY_PROVIDER: request.provider,
            wire.KEY_MODEL: request.model,
            wire.KEY_SYSTEM_SUFFIX: request.append_system_prompt,
            wire.KEY_ENV: dict(request.env),
            wire.KEY_ENV_FILES: _read_env_files(request.env_files, request.agent),
        }

    def _skill_ref(self, skill: str, request: SpawnRequest) -> dict[str, str]:
        path = Path(skill).resolve()
        profile = request.profile_dir.resolve()
        if path.is_relative_to(profile):
            return {
                wire.KEY_ROOT: wire.ROOT_PROFILE,
                wire.KEY_PATH: _relative(path, profile),
            }
        if self._library_root is not None and path.is_relative_to(self._library_root):
            return {
                wire.KEY_ROOT: wire.ROOT_LIBRARY,
                wire.KEY_PATH: _relative(path, self._library_root),
            }
        raise PiHostError(
            f"{request.agent} is given the skill at {skill}, which is neither in "
            f"its own profile nor in the shared skill library — those are the two "
            f"directories its host mounts, so nothing there could load it"
        )


class RemoteTransport:
    """The channel to a process running on another host. Same three methods the
    local transport offers, so nothing above this line can tell them apart."""

    def __init__(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        self._ws = ws
        self._detail = ""

    async def send(self, line: str) -> None:
        if self._ws.closed:
            raise BrokenPipeError("the connection to the runtime host is closed")
        await self._ws.send_bytes(line.encode("utf-8"))

    async def lines(self) -> AsyncIterator[str]:
        async for message in self._ws:
            if message.type is aiohttp.WSMsgType.BINARY:
                yield message.data.decode("utf-8", errors="replace")
            elif message.type is aiohttp.WSMsgType.TEXT:
                if not self._control(message.data):
                    break
            elif message.type is aiohttp.WSMsgType.ERROR:
                self._detail = f"connection error: {self._ws.exception()}"
                break
        if not self._detail:
            # The socket ended without the host saying why — a container that
            # was killed, a network that went away. Say that rather than let it
            # read as an ordinary exit.
            self._detail = "the connection to the runtime host closed unexpectedly"

    async def exit_detail(self) -> str:
        return self._detail

    async def aclose(self) -> None:
        if not self._ws.closed:
            await self._ws.close()

    def _control(self, raw: str) -> bool:
        """Handle one control frame. False = stop reading lines."""
        try:
            frame = json.loads(raw)
        except ValueError:
            self._detail = "the runtime host sent an unreadable control frame"
            return False
        kind = frame.get(wire.KEY_TYPE)
        if kind == wire.MSG_EXIT:
            self._detail = str(frame.get(wire.KEY_DETAIL) or "")
            return False
        if kind == wire.MSG_ERROR:
            self._detail = str(frame.get(wire.KEY_MESSAGE) or "the runtime host failed")
            return False
        logger.warning("Ignoring unknown control frame from runtime host: %r", kind)
        return True


def _decode(raw: str, agent: str) -> dict[str, object]:
    try:
        frame = json.loads(raw)
    except ValueError as exc:
        raise PiHostError(
            f"{agent}'s runtime host sent an unreadable control frame"
        ) from exc
    if not isinstance(frame, dict):
        raise PiHostError(f"{agent}'s runtime host sent a control frame that is not an object")
    return frame


def _relative(path: Path, root: Path) -> str:
    # "." for the root itself: a skill CAN be the whole mounted directory, and
    # posix form travels because the host's separator is not necessarily ours.
    relative = path.relative_to(root)
    return relative.as_posix() if relative.parts else "."


def _read_env_files(files: Mapping[str, Path], agent: str) -> dict[str, str]:
    """Ship the CONTENT of the files an env variable points at. The host writes
    its own copy and points the variable there; the path this side wrote means
    nothing over there."""
    content: dict[str, str] = {}
    for name, path in files.items():
        try:
            content[name] = Path(path).read_text(encoding="utf-8")
        except OSError as exc:
            raise PiHostError(
                f"cannot read {name} for {agent} ({path}): {exc}"
            ) from exc
    return content
