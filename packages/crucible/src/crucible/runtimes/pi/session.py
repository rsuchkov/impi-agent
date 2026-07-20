"""PiRpcSession: one conversation backed by a single pi RPC process."""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from crucible.ports.agent.ui import UiBridge, UiOutcome, UiRequest
from crucible.runtimes.pi import protocol
from crucible.runtimes.pi.errors import PiProcessError, PiProtocolError, PiTimeout
from crucible.runtimes.pi.protocol import PiEvent
from crucible.runtimes.pi.transport import PiTransport

logger = logging.getLogger(__name__)

EventCallback = Callable[[PiEvent], Awaitable[None] | None]

# extension_ui_request methods that expect a response; the rest are fire-and-forget.
_INTERACTIVE_UI_METHODS = frozenset({"select", "confirm", "input", "editor"})
# How often the turn-wait loop re-checks the deadline (also how fast it notices a
# UI wait starting/ending). Small enough to be responsive, large enough to be cheap.
_TURN_POLL_INTERVAL = 0.5


@dataclass
class PiResult:
    """Outcome of a single prompt/follow_up turn."""

    text: str = ""
    tool_calls: list[str] = field(default_factory=list)
    duration_s: float = 0.0
    stop_reason: str | None = None


class _Turn:
    """Mutable accumulator for one in-flight prompt turn."""

    def __init__(self, command_id: str) -> None:
        self.command_id = command_id
        self.future: asyncio.Future[PiResult] = asyncio.get_running_loop().create_future()
        self.text_parts: list[str] = []
        self.tool_calls: list[str] = []


class PiRpcSession:
    """Owns a pi process and exchanges JSONL prompts/events with it.

    One session = one conversation. Only one turn runs at a time; the runtime
    serializes concurrent messages on the same conversation.

    After a timeout the session is POISONED: a late agent_end from the aborted
    turn could otherwise complete the next turn, so every subsequent turn is
    refused and the owner must discard the session.
    """

    def __init__(
        self,
        transport: PiTransport,
        *,
        allowed_ui_tools: frozenset[str] = frozenset(),
        on_event: EventCallback | None = None,
        ui_bridge: UiBridge | None = None,
        session_id: str = "",
    ) -> None:
        self._transport = transport
        self._allowed_ui_tools = allowed_ui_tools
        self._on_event = on_event
        # When set, interactive UI requests are routed to a human via the bridge
        # instead of being auto-rejected; session_id tells the bridge which
        # conversation to ask. Without a bridge the auto-reject backstop applies.
        self._ui_bridge = ui_bridge
        self._session_id = session_id

        self._turn: _Turn | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._closed = False
        self._poisoned = False
        # Wall-clock spent waiting on a human this turn; the turn timeout excludes
        # it so a slow approver never poisons the session. _ui_wait_started is the
        # start of an in-flight wait (None when no UI request is outstanding).
        self._ui_wait_started: float | None = None
        self._ui_wait_total = 0.0

    @property
    def busy(self) -> bool:
        """True while a turn is in flight (the reaper must not close us)."""
        return self._turn is not None

    def start(self) -> None:
        """Begin draining stdout. Call once, before the first prompt."""
        if self._reader_task is None:
            self._reader_task = asyncio.ensure_future(self._read_loop())

    async def prompt(self, message: str, *, timeout: float) -> PiResult:
        return await self._run_turn(protocol.encode_prompt, message, timeout=timeout)

    async def follow_up(self, message: str, *, timeout: float) -> PiResult:
        return await self._run_turn(protocol.encode_follow_up, message, timeout=timeout)

    async def abort(self) -> None:
        await self._transport.send(
            protocol.encode_abort(command_id=protocol.new_command_id())
        )

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            await self._transport.aclose()
        finally:
            if self._reader_task is not None:
                self._reader_task.cancel()
            if self._turn is not None and not self._turn.future.done():
                self._turn.future.set_exception(
                    PiProcessError("Session closed during an in-flight turn")
                )

    # -- internals ----------------------------------------------------------

    async def _run_turn(
        self,
        encoder: Callable[..., str],
        message: str,
        *,
        timeout: float,
    ) -> PiResult:
        if self._closed:
            raise PiProcessError("Session is closed")
        if self._poisoned:
            raise PiProcessError(
                "Session is poisoned after a timeout; discard it and spawn a fresh one"
            )
        if self._turn is not None:
            raise RuntimeError("A turn is already in flight on this session")
        if self._reader_task is None:
            self.start()

        command_id = protocol.new_command_id()
        turn = _Turn(command_id)
        self._turn = turn
        self._ui_wait_started = None
        self._ui_wait_total = 0.0
        started = asyncio.get_running_loop().time()

        try:
            await self._transport.send(encoder(message, command_id=command_id))
            result = await self._await_turn(turn, timeout=timeout)
            result.duration_s = asyncio.get_running_loop().time() - started
            return result
        except asyncio.TimeoutError:
            logger.warning("pi turn timed out after %.1fs; aborting", timeout)
            self._poisoned = True
            await self._safe_abort()
            raise PiTimeout(f"pi did not finish within {timeout:.0f}s") from None
        finally:
            self._turn = None

    async def _await_turn(self, turn: _Turn, *, timeout: float) -> PiResult:
        """Await the turn result, PAUSING the timeout while a human is answering a
        UI request (``_ui_wait_started`` set). Polls so it notices a UI wait
        starting or ending; a genuinely stuck turn still times out on active time
        (wall-clock minus time spent waiting on people)."""
        loop = asyncio.get_running_loop()
        started = loop.time()
        while True:
            if turn.future.done():
                return turn.future.result()
            if self._ui_wait_started is None:
                active = (loop.time() - started) - self._ui_wait_total
                if active >= timeout:
                    raise asyncio.TimeoutError
            try:
                return await asyncio.wait_for(
                    asyncio.shield(turn.future), timeout=_TURN_POLL_INTERVAL
                )
            except asyncio.TimeoutError:
                continue  # re-evaluate: a UI wait may have started/ended

    async def _safe_abort(self) -> None:
        try:
            await self.abort()
        except Exception as exc:  # the process may already be gone
            logger.debug("abort after timeout failed: %s", exc)

    async def _read_loop(self) -> None:
        try:
            async for line in self._transport.lines():
                try:
                    event = protocol.parse_line(line)
                except PiProtocolError as exc:
                    # Log the size and both ends of the line — a truncated/unterminated
                    # line breaks near the tail, so head+tail pinpoints where.
                    logger.warning(
                        "Skipping unparseable pi line (%d chars): %s\n  head: %r\n  tail: %r",
                        len(line),
                        exc,
                        line[:200],
                        line[-300:],
                    )
                    logger.debug("Full unparseable pi line: %r", line)
                    continue
                await self._dispatch(event)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # transport blew up
            logger.exception("pi read loop crashed")
            self._fail_turn(PiProcessError(f"pi read loop crashed: {exc}"))
            return
        # EOF: process exited. Any in-flight turn must fail.
        self._fail_turn(PiProcessError("pi process exited unexpectedly"))

    def _fail_turn(self, exc: Exception) -> None:
        turn = self._turn
        if turn is not None and not turn.future.done():
            turn.future.set_exception(exc)

    async def _dispatch(self, event: PiEvent) -> None:
        await self._notify(event)

        if event.type == protocol.EV_EXTENSION_UI_REQUEST:
            await self._handle_ui_request(event)
            return

        turn = self._turn
        if turn is None:
            return

        if event.is_response and event.id == turn.command_id and event.success is False:
            self._fail_turn(PiProcessError(event.error or "pi rejected the prompt"))
            return

        if event.type == protocol.EV_MESSAGE_UPDATE:
            done = protocol.completed_text(event)
            if done is not None:
                turn.text_parts.append(done)
            return

        if event.type in (
            protocol.EV_TOOL_EXECUTION_START,
            protocol.EV_TOOL_EXECUTION_END,
        ):
            name = protocol.tool_name(event)
            if name and event.type == protocol.EV_TOOL_EXECUTION_START:
                turn.tool_calls.append(name)
            return

        if event.type == protocol.EV_AGENT_END:
            # pi auto-retries transient failures, emitting an agent_end with
            # willRetry=true per attempt; only the final one (willRetry falsy)
            # ends the turn.
            if event.raw.get("willRetry"):
                return
            self._complete_turn(turn, event)

    def _complete_turn(self, turn: _Turn, event: PiEvent) -> None:
        text = "".join(turn.text_parts).strip()
        if not text:
            text = _final_text_from_agent_end(event)
        # An LLM/infra failure (e.g. provider unreachable) yields empty content
        # with stopReason=error. Surface it as an error instead of masquerading
        # as an empty answer, so it is logged distinctly and not silently treated
        # like "no results".
        if not text:
            error = _error_from_agent_end(event)
            if error:
                self._fail_turn(PiProcessError(f"pi LLM error: {error}"))
                return
        result = PiResult(
            text=text,
            tool_calls=turn.tool_calls,
            stop_reason=_stop_reason_from_agent_end(event),
        )
        if not turn.future.done():
            turn.future.set_result(result)

    async def _handle_ui_request(self, event: PiEvent) -> None:
        """Answer a pi ``extension_ui_request``.

        Interactive methods (select/confirm/input/editor) expect a reply;
        fire-and-forget ones (notify, setStatus, setWidget, setTitle, ...) are
        ignored. With a ``UiBridge`` wired, an interactive request is surfaced to
        a human and their answer is sent back (the turn timeout pauses meanwhile);
        without one, the auto-reject backstop replies — allow only an allowlisted
        target — so a locked-down agent can't deadlock on approval.
        """
        request_id = event.id
        if request_id is None:
            return
        method = event.raw.get("method")
        if method not in _INTERACTIVE_UI_METHODS:
            return
        if self._ui_bridge is not None and self._session_id:
            reply = await self._bridge_ui_request(request_id, str(method), event)
        else:
            reply = self._auto_reject_reply(request_id, event)
        try:
            await self._transport.send(reply)
        except Exception as exc:
            logger.debug("failed to answer extension_ui_request: %s", exc)

    async def _bridge_ui_request(self, request_id: str, method: str, event: PiEvent) -> str:
        """Route an interactive request to the human via the bridge, accounting
        for the wait so the turn timeout excludes it. Always resolves."""
        assert self._ui_bridge is not None
        req = UiRequest(
            request_id=request_id,
            method=method,
            title=str(event.raw.get("title") or ""),
            message=str(event.raw.get("message") or ""),
            options=tuple(str(o) for o in (event.raw.get("options") or [])),
            placeholder=str(event.raw.get("placeholder") or ""),
        )
        loop = asyncio.get_running_loop()
        self._ui_wait_started = loop.time()
        try:
            outcome = await self._ui_bridge.request(self._session_id, req)
        except Exception:
            logger.exception("ui bridge failed; defaulting to a declined response")
            outcome = UiOutcome(cancelled=True)
        finally:
            if self._ui_wait_started is not None:
                self._ui_wait_total += loop.time() - self._ui_wait_started
                self._ui_wait_started = None
        if outcome.cancelled:
            return protocol.encode_extension_ui_response(request_id, cancelled=True)
        return protocol.encode_extension_ui_response(
            request_id, value=outcome.value, confirmed=outcome.confirmed
        )

    def _auto_reject_reply(self, request_id: str, event: PiEvent) -> str:
        """Backstop with no human bridge: allow only an allowlisted target, deny
        the rest, so a locked-down agent can't hang on approval that never comes."""
        target = protocol.tool_name(event) or _ui_target(event)
        allow = target in self._allowed_ui_tools
        return protocol.encode_extension_ui_response(
            request_id, value="Allow" if allow else "Block", confirmed=allow
        )

    async def _notify(self, event: PiEvent) -> None:
        if self._on_event is None:
            return
        try:
            result = self._on_event(event)
            if asyncio.iscoroutine(result):
                await result
        except Exception:
            logger.exception("on_event callback failed")


def _assistant_messages(event: PiEvent) -> list[dict]:
    messages = event.raw.get("messages")
    if not isinstance(messages, list):
        return []
    return [m for m in messages if isinstance(m, dict) and m.get("role") == "assistant"]


def _final_text_from_agent_end(event: PiEvent) -> str:
    """Best-effort extraction of assistant text from an agent_end payload.

    Content blocks include thinking/tool blocks — take only ``type=="text"``
    (a bare string content is legacy shorthand for one text block).
    """
    parts: list[str] = []
    for message in _assistant_messages(event):
        content = message.get("content")
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            for block in content:
                if (
                    isinstance(block, dict)
                    and block.get("type") == "text"
                    and isinstance(block.get("text"), str)
                ):
                    parts.append(block["text"])
    return "".join(parts).strip()


def _error_from_agent_end(event: PiEvent) -> str | None:
    """Return an LLM/infra error message from an agent_end payload, if any."""
    messages = event.raw.get("messages")
    if not isinstance(messages, list):
        return None
    for message in messages:
        if not isinstance(message, dict):
            continue
        if message.get("stopReason") == "error":
            return message.get("errorMessage") or "unknown error"
    return None


def _stop_reason_from_agent_end(event: PiEvent) -> str | None:
    reasons = [
        m.get("stopReason") for m in _assistant_messages(event) if m.get("stopReason")
    ]
    return reasons[-1] if reasons else None


def _ui_target(event: PiEvent) -> str | None:
    for key in ("toolName", "name", "target", "title"):
        value = event.raw.get(key)
        if isinstance(value, str):
            return value
    return None
