"""PiRuntime: maps session ids to pi sessions and runs prompts.

The concrete :class:`crucible.ports.agent.AgentRuntime` — platform-agnostic (a session
id is an opaque key chosen by the caller; the SQLite inventory and pi's on-disk
sessions stay in agreement because neither derives its own).

- ``run_stateful``  — one persistent pi session per conversation; pi keeps the
  memory on disk under ``session_id``. Turns on one session are serialized.
- ``run_stateless`` — a fresh ``--no-session`` process per call, closed right
  after; no memory.

A global semaphore bounds how many pi processes are alive at once; an optional
idle reaper closes sessions that have gone quiet (files survive — the next
message resumes the same pi session id with its memory intact).
"""

import asyncio
import logging
import os
import re
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from crucible.ports.agent.runtime import AgentProfile, PromptImage
from crucible.ports.agent.ui import UiBridge
from crucible.runtimes.pi.errors import PiProcessError, PiTimeout
from crucible.runtimes.pi.profiles import PiProfile
from crucible.runtimes.pi.session import EventCallback, PiResult, PiRpcSession
from crucible.runtimes.pi.transport import SubprocessTransport

logger = logging.getLogger(__name__)

# (profile, session_id_or_None, on_event, cwd_or_None) -> a started session
SessionFactory = Callable[
    [PiProfile, str | None, EventCallback | None, str | None], Awaitable[PiRpcSession]
]


@dataclass
class _ManagedSession:
    session: PiRpcSession
    created_at: float
    last_used: float = 0.0


def _require_pi_profile(profile: AgentProfile) -> PiProfile:
    """Narrow the opaque port profile at the driver boundary (no cast upstream)."""
    if not isinstance(profile, PiProfile):
        raise TypeError(f"PiRuntime needs a PiProfile, got {type(profile).__name__}")
    return profile


class PiRuntime:
    """Concrete ``AgentRuntime`` backed by ``pi --mode rpc``.

    Returns ``PiResult`` (an ``AgentResult``) and raises ``PiError``/``PiTimeout``
    (``AgentError``/``AgentTimeout`` subclasses), so flows depend only on the
    agent ports, never on pi.
    """

    def __init__(
        self,
        *,
        pi_bin: str = "pi",
        session_dir: str = "",
        max_concurrent_sessions: int = 4,
        idle_ttl: float = 1800.0,
        extra_env: dict[str, str] | None = None,
        extra_extensions: list[str] | None = None,
        session_factory: SessionFactory | None = None,
        ui_bridge: UiBridge | None = None,
    ) -> None:
        self._pi_bin = pi_bin
        self._session_dir = session_dir
        self._idle_ttl = idle_ttl
        # Surfaces a pi mid-turn interactive request (confirm/select) to a human;
        # injected into every stateful session so blocking UI dialogs round-trip
        # through Mattermost instead of being auto-rejected.
        self._ui_bridge = ui_bridge
        # Forwarded into every pi subprocess. .env is read by pydantic only, so
        # pi children would not otherwise see it.
        self._extra_env = extra_env or {}
        # Loaded via `-e` on every spawn. Provider extensions must load early
        # (before project trust), so they go here rather than in profile settings.
        self._extra_extensions = extra_extensions or []
        self._semaphore = asyncio.Semaphore(max_concurrent_sessions)
        self._factory = session_factory or self._spawn_session

        self._sessions: dict[str, _ManagedSession] = {}
        # Lock objects are NEVER removed: a waiter queued on a dropped lock and
        # a newcomer on a fresh one would otherwise run two turns concurrently
        # on the same conversation.
        self._locks: dict[str, asyncio.Lock] = {}
        self._reaper_task: asyncio.Task[None] | None = None

    # -- public API (AgentRuntime) -------------------------------------------

    async def run_stateful(
        self,
        profile: AgentProfile,
        session_id: str,
        message: str,
        *,
        on_event: EventCallback | None = None,
        cwd: str | None = None,
        images: Sequence[PromptImage] = (),
    ) -> PiResult:
        pi_profile = _require_pi_profile(profile)
        lock = self._locks.setdefault(session_id, asyncio.Lock())
        async with lock:
            managed = self._sessions.get(session_id)
            if managed is None:
                # on_event binds at session creation and lives as long as the
                # session (one conversation = one stable callback).
                session = await self._create_session(
                    pi_profile, session_id=session_id, on_event=on_event, cwd=cwd
                )
                managed = _ManagedSession(session=session, created_at=self._now())
                self._sessions[session_id] = managed

            try:
                # Every turn uses `prompt` — the pi session keeps the running
                # conversation. (pi's `follow_up` only queues a message while
                # the agent is running; sent to an idle agent it never starts a
                # turn and hangs until timeout.)
                result = await managed.session.prompt(
                    message, timeout=pi_profile.timeout, images=images
                )
            except (PiTimeout, PiProcessError):
                # A dead/stuck/poisoned session can't be reused; drop it so the
                # next message spawns a fresh one (pi-side memory for this
                # conversation resumes from disk under the same session id).
                await self._drop_session(session_id)
                raise

            managed.last_used = self._now()
            logger.info(
                "pi turn: session=%s profile=%s %.1fs tools=%d text=%d chars stop=%s",
                session_id,
                pi_profile.name,
                result.duration_s,
                len(result.tool_calls),
                len(result.text),
                result.stop_reason,
            )
            return result

    async def run_stateless(
        self,
        profile: AgentProfile,
        message: str,
        *,
        on_event: EventCallback | None = None,
        images: Sequence[PromptImage] = (),
    ) -> PiResult:
        pi_profile = _require_pi_profile(profile)
        session = await self._create_session(
            pi_profile, session_id=None, on_event=on_event, cwd=None
        )
        try:
            return await session.prompt(
                message, timeout=pi_profile.timeout, images=images
            )
        finally:
            await self._close_session(session)

    def start(self) -> None:
        """AgentRuntime lifecycle: begin background maintenance (the reaper)."""
        self.start_reaper()

    def start_reaper(self, interval: float = 60.0) -> None:
        if self._reaper_task is None:
            self._reaper_task = asyncio.ensure_future(self._reap_loop(interval))

    async def close(self) -> None:
        if self._reaper_task is not None:
            self._reaper_task.cancel()
            self._reaper_task = None
        for key in list(self._sessions):
            await self._drop_session(key)

    async def drop_agent_sessions(self, agent: str) -> int:
        """Drop an agent's idle sessions so its next turn respawns pi with fresh
        config (new CLI flags + reloaded .pi/*). Used by hot-reload: pi bakes
        config at spawn, so a live process keeps the old config until it dies.

        Busy sessions (in-flight turn) are left alone — the reaper collects them
        once they idle. On-disk pi memory survives a drop, so the respawn resumes
        the same conversation. Returns how many sessions were dropped.
        """
        # The runtime_session_id is "<agent>--<conversation>"; the "--" separator
        # keeps the prefix from matching a different agent whose name is a prefix
        # of this one.
        prefix = f"{agent}--"
        stale = [
            key
            for key, managed in self._sessions.items()
            if key.startswith(prefix) and not managed.session.busy
        ]
        for key in stale:
            logger.info("Dropping pi session %s for reload", key)
            await self._drop_session(key)
        return len(stale)

    # -- internals ----------------------------------------------------------

    async def _create_session(
        self,
        profile: PiProfile,
        *,
        session_id: str | None,
        on_event: EventCallback | None,
        cwd: str | None = None,
    ) -> PiRpcSession:
        await self._semaphore.acquire()
        try:
            session = await self._factory(profile, session_id, on_event, cwd)
            session.start()
            return session
        except Exception:
            self._semaphore.release()
            raise

    async def _close_session(self, session: PiRpcSession) -> None:
        try:
            await session.close()
        finally:
            self._semaphore.release()

    async def _drop_session(self, key: str) -> None:
        managed = self._sessions.pop(key, None)
        if managed is not None:
            await self._close_session(managed.session)

    async def _spawn_session(
        self,
        profile: PiProfile,
        session_id: str | None,
        on_event: EventCallback | None,
        cwd: str | None = None,
    ) -> PiRpcSession:
        # --approve: RPC mode shows no trust prompt and otherwise ignores the
        # project's .pi/ resources (SYSTEM.md, permission policy). Approving
        # for the run loads them from the agent's profile dir.
        args = ["--mode", "rpc", "--approve"]
        if session_id:
            # pi requires [A-Za-z0-9._-] starting/ending alphanumeric; sanitize
            # defensively (collisions are mitigated by the per-agent session dir
            # and the agent prefix baked into the id by the caller).
            args += ["--session-id", _safe_session_id(session_id)]
        else:
            args += ["--no-session"]
        if self._session_dir:
            # Per-agent subdir (sanitized ids can collide across agents), and an
            # ABSOLUTE path — pi resolves a relative --session-dir from its own
            # cwd (the profile dir), scattering session files into the agents dir.
            session_dir = (Path(self._session_dir) / profile.name).resolve()
            session_dir.mkdir(parents=True, exist_ok=True)
            args += ["--session-dir", str(session_dir)]
        for ext in self._extra_extensions:
            args += ["-e", ext]
        # Single capability gate: --tools is the allowlist over built-in,
        # extension and typed tools alike. An empty list yields no tools at all;
        # a built-in an agent wants (e.g. read/bash for skills) is just named in
        # profile.tools.
        args += ["--tools", ",".join(profile.tools)]
        # No ambient skill discovery — each agent gets EXACTLY its declared
        # skills (this also closes the ancestor-dir .agents/skills + context
        # file walk-up the agents directory would otherwise leak).
        args += ["--no-skills"]
        for skill in profile.skills:
            args += ["--skill", skill]
        if profile.provider:
            args += ["--provider", profile.provider]
        if profile.model:
            args += ["--model", profile.model]
        # Extra system-prompt text (e.g. the gateway's response-formatting rules).
        if profile.append_system_prompt:
            args += ["--append-system-prompt", profile.append_system_prompt]

        # Shared env, then this agent's per-profile env (tool token), then this
        # session's id — so a tool call can be tied back to its conversation.
        per_session = {"RUNTIME_SESSION_ID": _safe_session_id(session_id)} if session_id else {}
        env: dict[str, str] | None = None
        if self._extra_env or profile.env or per_session:
            env = {**os.environ, **self._extra_env, **profile.env, **per_session}
        # cwd override for checkout-scoped runs; else the profile dir, so pi
        # natively loads the agent's .pi/*.
        run_cwd = cwd or str(profile.config_dir)
        transport = await SubprocessTransport.spawn(
            self._pi_bin, args, cwd=run_cwd, env=env
        )
        return PiRpcSession(
            transport,
            on_event=on_event,
            ui_bridge=self._ui_bridge,
            session_id=session_id or "",
        )

    async def _reap_loop(self, interval: float) -> None:
        try:
            while True:
                await asyncio.sleep(interval)
                await self._reap_once()
        except asyncio.CancelledError:
            raise

    async def _reap_once(self) -> None:
        cutoff = self._now() - self._idle_ttl
        stale = [
            key
            for key, managed in self._sessions.items()
            # Never-used sessions age by created_at (last_used == 0 must not
            # mean immortal); busy sessions are skipped mid-turn.
            if not managed.session.busy
            and (managed.last_used or managed.created_at) < cutoff
        ]
        for key in stale:
            logger.info("Reaping idle pi session %s", key)
            await self._drop_session(key)

    @staticmethod
    def _now() -> float:
        return asyncio.get_running_loop().time()


def _safe_session_id(raw: str) -> str:
    """Coerce a conversation key into a valid pi session id ([A-Za-z0-9._-])."""
    cleaned = re.sub(r"[^A-Za-z0-9._-]", "-", raw).strip("-._")
    return cleaned or "session"
