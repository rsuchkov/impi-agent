"""ToolGate: the server-side half of "are you sure?" for a tool call.

A tool may declare ``requires_confirmation``, and until now that was enforced in
one place only — the runtime's tool extension, which asks before it makes the
call. That gate is worth keeping for the latency it saves, but it cannot be the
only one: the token the extension authenticates with lives in the agent's own
environment, so a shell in that container can reach the tool server directly and
skip the question entirely. This is the gate that cannot be skipped, because it
sits inside the server that does the work.

It also answers the complaint that made the old gate tiring to live with: it
asked every single time. Here a human can say "yes, for the next fifteen
minutes", and the window is the same kind of window a secret uses — same table,
same ladder, same revocation.

Who may answer is deliberately *anyone in the conversation*, which is what the
blocking confirm has always done. A tool call is addressed to the people
watching the agent work; a credential is addressed to a named approver. That
difference is one argument to the shared registry.
"""

import asyncio
import json
import logging
import secrets as tokens
from datetime import datetime, timedelta, timezone
from typing import Any

from crucible.approvals import (
    Approval,
    PendingApprovals,
    approval_actions,
    humanize,
    render_card,
    windows,
)
from crucible.interactions.presence import AgentPresence
from crucible.interactions.service import conversation_ref
from crucible.store.base import (
    DECISION_APPROVED_GRANT,
    DECISION_APPROVED_ONCE,
    DECISION_DENIED,
    DECISION_NO_APPROVER,
    DECISION_REUSED_GRANT,
    DECISION_TIMEOUT,
    KIND_TOOL,
    ApprovalAudit,
    ApprovalGrant,
    ApprovalStore,
    SessionStore,
)

logger = logging.getLogger(__name__)

_ANSWERED = "⚙️ {verdict} — **{agent}** running `{tool}`."
_EXPIRED = "⌛ Nobody answered in time, so the call was refused."


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class ToolGate:
    def __init__(
        self,
        presence: AgentPresence,
        sessions: SessionStore,
        ledger: ApprovalStore,
        approvals: PendingApprovals,
        *,
        callback_url: str = "",
        timeout_s: float = 90.0,
        max_grant_s: int = 900,
    ) -> None:
        self._presence = presence
        self._sessions = sessions
        self._ledger = ledger
        self._approvals = approvals
        self._callback_url = callback_url
        self._timeout = timeout_s
        # Shorter than a secret's ceiling by default: "let it use bash for a
        # while" is a broader permission than one named credential.
        self._max_grant_s = max_grant_s

    async def confirm(
        self, agent: str, tool: str, args: dict[str, Any], *, runtime_session_id: str
    ) -> bool:
        started = asyncio.get_running_loop().time()
        request_id = f"rq_{tokens.token_hex(6)}"

        grant = await self._ledger.live_grant(KIND_TOOL, agent, tool, now=_now())
        if grant is not None:
            await self._record(
                agent, tool, args, DECISION_REUSED_GRANT, started, request_id,
                grant_id=grant.id,
            )
            return True

        answer = await self._ask(agent, tool, args, runtime_session_id)
        if answer is None:
            # Nowhere to ask. Fail closed, and say so — an engine whose
            # interactivity is off should not be silently running gated tools.
            await self._record(agent, tool, args, DECISION_NO_APPROVER, started, request_id)
            return False
        if not answer.allowed:
            decision = DECISION_TIMEOUT if answer.timed_out else DECISION_DENIED
            await self._record(
                agent, tool, args, decision, started, request_id, approver=answer.approver
            )
            return False

        grant_id = ""
        decision = DECISION_APPROVED_ONCE
        if answer.grant_s > 0:
            grant_id = await self._open_window(agent, tool, answer)
            decision = DECISION_APPROVED_GRANT
        await self._record(
            agent, tool, args, decision, started, request_id,
            approver=answer.approver, grant_id=grant_id,
        )
        return True

    async def _ask(
        self, agent: str, tool: str, args: dict[str, Any], runtime_session_id: str
    ) -> Approval | None:
        record = await self._sessions.get_by_runtime_session(runtime_session_id)
        poster = self._presence.poster(agent)
        if record is None or poster is None:
            logger.warning("tool gate: nowhere to ask about %s for %s", tool, agent)
            return None

        token = tokens.token_hex(16)
        future = self._approvals.register(
            token, kind=KIND_TOOL, principal=agent, scopes=(tool,)
        )
        try:
            post_id = await poster.post_actions(
                conversation_ref(record),
                _card(agent, tool, args),
                approval_actions(token, offers=windows(ceiling_s=self._max_grant_s)),
                callback_url=self._callback_url,
            )
        except Exception:
            self._approvals.discard(token)
            logger.warning("tool gate: could not post the question", exc_info=True)
            return None

        try:
            answer = await asyncio.wait_for(future, timeout=self._timeout)
        except TimeoutError:
            self._approvals.discard(token)
            await self._rewrite(poster, post_id, _EXPIRED)
            return Approval(allowed=False, timed_out=True)
        await self._rewrite(poster, post_id, _verdict(agent, tool, answer))
        return answer

    async def _open_window(self, agent: str, tool: str, answer: Approval) -> str:
        seconds = min(answer.grant_s, self._max_grant_s)
        now = datetime.now(timezone.utc)
        grant = ApprovalGrant(
            id=f"gr_{tokens.token_hex(6)}",
            kind=KIND_TOOL,
            principal=agent,
            scope=tool,
            granted_by=answer.approver,
            granted_at=now.isoformat(timespec="seconds"),
            expires_at=(now + timedelta(seconds=seconds)).isoformat(timespec="seconds"),
        )
        await self._ledger.create_grant(grant)
        logger.info(
            "tool %s: %s may run it for %s (granted by %s)",
            tool, agent, humanize(seconds), answer.approver,
        )
        return grant.id

    async def _record(
        self, agent: str, tool: str, args: dict[str, Any], decision: str,
        started: float, request_id: str, *, approver: str = "", grant_id: str = "",
    ) -> None:
        elapsed = asyncio.get_running_loop().time() - started
        await self._ledger.record_decision(
            ApprovalAudit(
                id=f"au_{tokens.token_hex(8)}",
                at=_now(),
                kind=KIND_TOOL,
                principal=agent,
                scope=tool,
                reason="",
                detail=_arguments(args),
                decision=decision,
                approver=approver,
                grant_id=grant_id,
                request_id=request_id,
                duration_ms=int(elapsed * 1000),
            )
        )
        logger.info("tool %s for %s: %s", tool, agent, decision)

    @staticmethod
    async def _rewrite(poster, post_id: str, text: str) -> None:
        """Retire the question. Best-effort: the decision is already made, and a
        platform hiccup here must not undo it."""
        if not post_id:
            return
        try:
            await poster.retract(post_id, text)
        except Exception:
            logger.warning("tool gate: could not retire %s", post_id, exc_info=True)


def _arguments(args: dict[str, Any]) -> str:
    """The call's arguments as one line a human can read.

    Rendered by the card's own hardening, so an argument cannot add structure to
    the question it appears in.
    """
    try:
        return json.dumps(args, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        return str(args)


def _card(agent: str, tool: str, args: dict[str, Any]) -> str:
    return render_card(
        f"⚙️ **{agent}** wants to run `{tool}`.",
        [],
        block_label="Arguments" if args else "",
        block=_arguments(args) if args else "",
    )


def _verdict(agent: str, tool: str, answer: Approval) -> str:
    if not answer.allowed:
        verdict = "Denied"
    elif answer.grant_s > 0:
        verdict = f"Allowed for {humanize(answer.grant_s)}"
    else:
        verdict = "Allowed once"
    return _ANSWERED.format(verdict=verdict, agent=agent, tool=tool)
