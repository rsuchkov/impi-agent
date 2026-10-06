"""ToolGate: the "are you sure?" in front of a tool call.

A tool may declare ``requires_confirmation``; this is the one place that asks.
It sits inside the server that does the work, so it cannot be skipped: the
token the runtime's tool extension authenticates with lives in the agent's own
environment, and a shell in that container could reach the tool server directly
— which is why the extension itself asks nothing. One call, one question.

A human can say "yes, for the next fifteen minutes": the window is the same kind
of window a secret uses — same table, same ladder, same revocation.

Who may answer: the people the deployment names (``approvers``), or — when it
names nobody — the person whose message started the turn. The model reads
text strangers wrote, and in a shared channel the stranger who talked it into
a call must not be the one who approves it; the person who asked the agent is
the one the question is addressed to. Precisely: the person whose message the turn answers (in a burst of messages merged
into one turn, the newest one's author; a scheduled task's turn, the person
who scheduled it); a conversation that names no person at all is answered
by anyone in it. A memoryless run has no conversation to post a card in, so
a gated tool there is refused outright.
`TOOL_APPROVERS` is one set for every agent and gateway — name ids from each
platform the agents run on. A credential, by contrast, is always addressed to
named approvers.
That difference is one argument to the shared registry.

And a window is that conversation's. The people who were asked are the people
it covers: "allow for fifteen minutes" said in one thread does not let the same
tool run unasked in another, where someone else may be talking to the agent
and nobody saw the question. So the window's scope is the tool *in* the
conversation, not the tool alone.
"""

import asyncio
import json
import logging
import secrets as tokens
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Any

from crucible.approvals import (
    Approval,
    PendingApprovals,
    approval_actions,
    humanize,
    windows,
)
from crucible.interactions.presence import AgentPresence
from crucible.interactions.service import conversation_ref
from crucible.ports.chat.types import ApprovalCard, CallPreview, PreviewRow
from crucible.store.base import (
    DECISION_ABANDONED,
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

# What the card's headline is rewritten to once the question is closed. The
# card keeps its facts — it is the history of what was decided about what.
_ANSWERED = "— {verdict}: {tool}."
_EXPIRED = "— ⌛ nobody answered in time, so {tool} was refused."
_ABANDONED = "— ⌛ {tool} was withdrawn before anyone answered."
# The raw arguments on a card without a preview, at most. A platform refuses a
# message past a few thousand characters, and a refused card is a refused call;
# the model's own account of a long argument is not worth that.
ARGUMENTS_LIMIT = 2000
# The preview's one-line account of the call, as the card's first row.
ACTION_LABEL = "Action"


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
        approvers: frozenset[str] = frozenset(),
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
        # Who may answer every card, by platform user id. Empty = the person
        # whose message started the turn.
        self._approvers = approvers

    async def confirm(
        self,
        agent: str,
        tool: str,
        args: dict[str, Any],
        *,
        runtime_session_id: str,
        preview: CallPreview | None = None,
    ) -> bool:
        started = asyncio.get_running_loop().time()
        request_id = f"rq_{tokens.token_hex(6)}"
        scope = tool_scope(tool, runtime_session_id)

        grant = await self._ledger.live_grant(KIND_TOOL, agent, scope, now=_now())
        if grant is not None:
            await self._record(
                agent, scope, args, DECISION_REUSED_GRANT, started, request_id,
                grant_id=grant.id,
            )
            return True

        try:
            answer = await self._ask(agent, tool, args, runtime_session_id, preview)
        except asyncio.CancelledError:
            # The caller hung up — the turn was aborted or timed out while the
            # card was up. Nothing may run on this card now, whatever is clicked.
            await self._record(agent, scope, args, DECISION_ABANDONED, started, request_id)
            raise
        if answer is None:
            # Nowhere to ask. Fail closed, and say so — an engine whose
            # interactivity is off should not be silently running gated tools.
            await self._record(agent, scope, args, DECISION_NO_APPROVER, started, request_id)
            return False
        if not answer.allowed:
            decision = DECISION_TIMEOUT if answer.timed_out else DECISION_DENIED
            await self._record(
                agent, scope, args, decision, started, request_id, approver=answer.approver
            )
            return False

        grant_id = ""
        decision = DECISION_APPROVED_ONCE
        if answer.grant_s > 0:
            grant_id = await self._open_window(agent, tool, scope, answer)
            decision = DECISION_APPROVED_GRANT
        await self._record(
            agent, scope, args, decision, started, request_id,
            approver=answer.approver, grant_id=grant_id,
        )
        return True

    async def _ask(
        self,
        agent: str,
        tool: str,
        args: dict[str, Any],
        runtime_session_id: str,
        preview: CallPreview | None,
    ) -> Approval | None:
        record = await self._sessions.get_by_runtime_session(runtime_session_id)
        poster = self._presence.poster(agent)
        if record is None or poster is None:
            logger.warning("tool gate: nowhere to ask about %s for %s", tool, agent)
            return None

        token = tokens.token_hex(16)
        # The record's last user is whoever's message this turn answers (the
        # newest of a merged burst; a scheduled turn's creator). A record that
        # never had a user leaves the set empty: anyone in the conversation.
        who = self._approvers or (
            frozenset({record.last_user_id}) if record.last_user_id else frozenset()
        )
        future = self._approvals.register(
            token, kind=KIND_TOOL, principal=agent, scopes=(tool,), approvers=who
        )
        expires_at = (
            datetime.now(timezone.utc) + timedelta(seconds=self._timeout)
        ).isoformat(timespec="seconds")
        card = _card(agent, tool, args, preview, expires_at=expires_at)
        try:
            post_id = await poster.post_confirmation(
                conversation_ref(record),
                card,
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
            await self._rewrite(poster, post_id, _closed(card, _EXPIRED.format(tool=tool)))
            return Approval(allowed=False, timed_out=True)
        except asyncio.CancelledError:
            # Retire the card first: a click after this must find no question.
            self._approvals.discard(token)
            await self._rewrite(poster, post_id, _closed(card, _ABANDONED.format(tool=tool)))
            raise
        await self._rewrite(poster, post_id, _closed(card, _verdict(tool, answer)))
        return answer

    async def _open_window(self, agent: str, tool: str, scope: str, answer: Approval) -> str:
        seconds = min(answer.grant_s, self._max_grant_s)
        now = datetime.now(timezone.utc)
        grant = ApprovalGrant(
            id=f"gr_{tokens.token_hex(6)}",
            kind=KIND_TOOL,
            principal=agent,
            scope=scope,
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
        self, agent: str, scope: str, args: dict[str, Any], decision: str,
        started: float, request_id: str, *, approver: str = "", grant_id: str = "",
    ) -> None:
        elapsed = asyncio.get_running_loop().time() - started
        await self._ledger.record_decision(
            ApprovalAudit(
                id=f"au_{tokens.token_hex(8)}",
                at=_now(),
                kind=KIND_TOOL,
                principal=agent,
                scope=scope,
                reason="",
                detail=_arguments(args),
                decision=decision,
                approver=approver,
                grant_id=grant_id,
                request_id=request_id,
                duration_ms=int(elapsed * 1000),
            )
        )
        logger.info("tool %s for %s: %s", scope, agent, decision)

    @staticmethod
    async def _rewrite(poster, post_id: str, card: ApprovalCard) -> None:
        """Retire the question by rewriting the card with its verdict — still a
        card, drawn the same way, nothing left to click. Best-effort: the
        decision is already made, and a platform hiccup here must not undo it."""
        if not post_id:
            return
        try:
            await poster.update_confirmation(post_id, card)
        except Exception:
            logger.warning("tool gate: could not retire %s", post_id, exc_info=True)


def tool_scope(tool: str, runtime_session_id: str) -> str:
    """What a window covers, as the ledger spells it: this tool, in this
    conversation. The conversation is named by its runtime session id — the one
    key the gate is handed — so the same window is found again by the same call."""
    return f"{tool}@{runtime_session_id}"


def _arguments(args: dict[str, Any]) -> str:
    """The call's arguments as one line a human can read.

    Rendered by the card's own hardening, so an argument cannot add structure to
    the question it appears in.
    """
    try:
        rendered = json.dumps(args, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        rendered = str(args)
    if len(rendered) > ARGUMENTS_LIMIT:
        rendered = rendered[: ARGUMENTS_LIMIT - 1] + "…"
    return rendered


def _card(
    agent: str,
    tool: str,
    args: dict[str, Any],
    preview: CallPreview | None = None,
    *,
    expires_at: str = "",
) -> ApprovalCard:
    """The request as data. With a preview: what the tool says the call would
    do — its one-line account as the first row, then row by row, a change as
    ``before → value``. Without one: the arguments, verbatim, as the card's
    block. The headline is the engine's sentence and nothing else goes in it:
    the title, like every value, is the tool's text and takes the channel's
    hardening as a field, where a line break or a mention cannot restyle the
    card. Each channel renders it."""
    if preview is None:
        return ApprovalCard(
            agent=agent,
            headline=f"wants to run {tool}.",
            block_label="Arguments" if args else "",
            block=_arguments(args) if args else "",
            expires_at=expires_at,
        )
    title = (PreviewRow(ACTION_LABEL, preview.title),) if preview.title else ()
    return ApprovalCard(
        agent=agent,
        headline=f"wants to run {tool}.",
        icon="⚠️" if preview.danger else "⚙️",
        fields=title + tuple(preview.rows),
        danger=preview.danger,
        expires_at=expires_at,
    )


def _closed(card: ApprovalCard, headline: str) -> ApprovalCard:
    """The card once the question is over: same facts, the verdict for a
    headline, no deadline left to count down to."""
    return replace(card, headline=headline, expires_at="")


def _verdict(tool: str, answer: Approval) -> str:
    if not answer.allowed:
        verdict = "Denied"
    elif answer.grant_s > 0:
        verdict = f"Allowed for {humanize(answer.grant_s)}"
    else:
        verdict = "Allowed once"
    return _ANSWERED.format(verdict=verdict, tool=tool)
