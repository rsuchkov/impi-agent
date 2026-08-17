"""SecretBroker: the decision between an agent asking for a credential and
getting one.

The order of the checks is the design. Policy is consulted first, from the
store, without touching the backend — so a request naming something that isn't
configured costs nothing and reveals nothing. Only a request that passes policy
reaches the backend, and only one that also passes the human reaches a value.

Every path ends in a ledger row and in a caller who is told one of two things.
The refusals are deliberately indistinguishable: an agent that could tell "no
such secret" from "not yours" could map the store by guessing names.
"""

import asyncio
import logging
import secrets as tokens
import time
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone

from crucible.approvals.card import command_line
from crucible.ports.chat.admin import ChatAdmin
from crucible.ports.chat.types import ConversationRef
from crucible.secrets.approvals import (
    Approval,
    SecretApprovals,
    approval_actions,
    approval_text,
    humanize,
)
from crucible.secrets.policy import ASK, REFUSE, evaluate, grant_options
from crucible.secrets.ports import (
    AgentPosters,
    BackendStatus,
    LeaseRequest,
    LeaseResult,
    SecretBackend,
    SecretBackendError,
    UnlockMaterial,
)
from crucible.store.base import (
    DECISION_APPROVED_GRANT,
    DECISION_APPROVED_ONCE,
    DECISION_BACKEND_ERROR,
    DECISION_DENIED,
    DECISION_LOCKED,
    DECISION_NO_APPROVER,
    DECISION_REUSED_GRANT,
    DECISION_SEALED,
    DECISION_TIMEOUT,
    KIND_SECRET,
    ApprovalAudit,
    ApprovalGrant,
    ApprovalStore,
    SecretPolicyRecord,
    SecretPolicyStore,
)

logger = logging.getLogger(__name__)

# The card is rewritten once it has been answered, so a second click finds no
# buttons and the ledger cannot disagree with what the message says happened.
_ANSWERED = "🔐 {verdict} — asked by **{agent}** for `{secret}`."
_EXPIRED = "⌛ Nobody answered in time, so the request was refused."

# Kept short enough to stay readable in a chat card and in a ledger row; the
# caller's own output is where the full story lives.
_MAX_REASON = 200


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class SecretBroker:
    def __init__(
        self,
        backend: SecretBackend,
        policies: SecretPolicyStore,
        ledger: ApprovalStore,
        presence: AgentPosters,
        admins: Mapping[str, ChatAdmin],
        approvals: SecretApprovals,
        *,
        approvers: str = "",
        approval_channel: str = "",
        approval_timeout_s: float = 120.0,
        max_grant_s: int = 3600,
        callback_url: str = "",
    ) -> None:
        self._backend = backend
        self._policies = policies
        # Windows and the ledger are not secret-shaped — the same two tables
        # serve every other thing a human can authorize.
        self._ledger = ledger
        self._presence = presence
        self._admins = admins
        self._approvals = approvals
        self._approvers_raw = approvers
        self._approval_channel = approval_channel
        self._timeout = approval_timeout_s
        self._max_grant_s = max_grant_s
        self._callback_url = callback_url
        # Usernames resolve to ids once. The map is small and stable; re-reading
        # it per request would put a directory lookup in the path of every
        # credential the engine hands out.
        self._approver_ids: frozenset[str] | None = None

    # -- the decision ---------------------------------------------------------

    async def lease(self, request: LeaseRequest) -> LeaseResult:
        started = time.monotonic()
        # One id per invocation: a request may leave several ledger rows, and an
        # operator reading them needs to see which belong together.
        request_id = f"rq_{tokens.token_hex(6)}"
        name = request.secret  # ValueError -> a malformed request, not a refusal
        policy = await self._policies.get_policy(name)
        verdict = evaluate(policy, request.agent)
        if verdict.outcome == REFUSE:
            return await self._refuse(request, verdict.decision, started, request_id)

        state = await self._backend.status()
        if not state.usable:
            return await self._refuse(request, _unusable(state), started, request_id)

        decision, approver, grant = verdict.decision, "", None
        if verdict.outcome == ASK:
            assert policy is not None  # ASK is only reachable with a policy
            grant = await self._ledger.live_grant(KIND_SECRET, request.agent, name, now=_now())
            if grant is not None:
                decision = DECISION_REUSED_GRANT
            else:
                answer = await self._ask(request, policy)
                if answer is None:
                    return await self._refuse(request, DECISION_NO_APPROVER, started, request_id)
                approver = answer.approver
                if not answer.allowed:
                    refusal = DECISION_TIMEOUT if answer.timed_out else DECISION_DENIED
                    return await self._refuse(request, refusal, started, request_id, approver=approver)
                if answer.grant_s > 0:
                    grant = await self._open_window(request, policy, answer)
                    decision = DECISION_APPROVED_GRANT
                else:
                    decision = DECISION_APPROVED_ONCE

        grant_id = grant.id if grant is not None else ""
        try:
            values = {
                env_name: await self._backend.read(ref) for env_name, ref in request.bindings
            }
        except SecretBackendError as exc:
            failure = DECISION_SEALED if exc.sealed else DECISION_BACKEND_ERROR
            logger.warning("secret %s for %s: %s", name, request.agent, exc)
            return await self._refuse(
                request, failure, started, request_id, approver=approver, grant_id=grant_id
            )

        await self._record(
            request, decision, started, request_id, approver=approver, grant_id=grant_id
        )
        return LeaseResult(granted=True, decision=decision, values=values)

    # -- asking ---------------------------------------------------------------

    async def _ask(
        self, request: LeaseRequest, policy: SecretPolicyRecord
    ) -> Approval | None:
        """Put the request in front of a human. None when there was nobody to
        put it in front of — a missing approver is a configuration problem, and
        must not read as a refusal in the ledger."""
        approvers = await self._resolve_approvers(request.agent)
        poster = self._presence.poster(request.agent)
        if not approvers or poster is None:
            logger.warning(
                "secret %s for %s: nobody to ask (approvers=%d, poster=%s)",
                request.secret, request.agent, len(approvers), poster is not None,
            )
            return None
        channel = await self._approval_conversation(request.agent, approvers)
        if not channel:
            return None

        token = tokens.token_hex(16)
        windows = grant_options(policy, ceiling_s=self._max_grant_s)
        future = self._approvals.register(
            token, agent=request.agent, secrets=(request.secret,), approvers=approvers
        )
        ref = ConversationRef(
            channel_id=channel, conversation_id=channel, message_id=channel
        )
        try:
            post_id = await poster.post_actions(
                ref,
                approval_text(
                    request.agent,
                    references=request.references,
                    reason=request.reason[:_MAX_REASON],
                    command=request.command,
                ),
                approval_actions(token, windows=windows),
                callback_url=self._callback_url,
            )
        except Exception:
            self._approvals.discard(token)
            logger.warning("secret approval could not be posted", exc_info=True)
            return None

        try:
            answer = await asyncio.wait_for(future, timeout=self._timeout)
        except TimeoutError:
            self._approvals.discard(token)
            await self._rewrite(poster, post_id, _EXPIRED)
            return Approval(allowed=False, timed_out=True)
        await self._rewrite(poster, post_id, _verdict_line(request, answer))
        return answer

    async def _approval_conversation(self, agent: str, approvers: frozenset[str]) -> str:
        """Where the card goes. A configured channel wins; otherwise the agent
        opens a one-to-one conversation with an approver, because a request that
        lands in a shared channel is a request everyone present can read."""
        if self._approval_channel:
            return self._approval_channel
        admin = self._admins.get(agent)
        if admin is None:
            logger.warning("secret approval: %s cannot open a direct conversation", agent)
            return ""
        for user_id in sorted(approvers):
            channel = await admin.open_direct(user_id)
            if channel:
                return channel
        return ""

    async def _resolve_approvers(self, agent: str) -> frozenset[str]:
        """The configured approvers as platform user ids.

        Entries may be written either way. An entry the directory recognizes as
        a username becomes its id; anything else is taken to be an id already,
        so a deployment whose platform has no username lookup still works.
        """
        if self._approver_ids is not None:
            return self._approver_ids
        entries = [e.strip().lstrip("@") for e in self._approvers_raw.split(",") if e.strip()]
        admin = self._admins.get(agent)
        resolved: set[str] = set()
        for entry in entries:
            found = await admin.resolve_username(entry) if admin is not None else None
            resolved.add(found or entry)
        self._approver_ids = frozenset(resolved)
        return self._approver_ids

    @staticmethod
    async def _rewrite(poster, post_id: str, text: str) -> None:
        """Retire the card. Best-effort: the decision has already been made, and
        a platform hiccup here must not undo it."""
        if not post_id:
            return
        try:
            await poster.retract(post_id, text)
        except Exception:
            logger.warning("could not retire the approval card %s", post_id, exc_info=True)

    async def _open_window(
        self, request: LeaseRequest, policy: SecretPolicyRecord, answer: Approval
    ) -> ApprovalGrant:
        """Record the window a human opened, capped by the policy and by the
        deployment — the ladder is filtered before it is shown, and capped again
        here so a hand-made callback cannot ask for a year."""
        seconds = min(answer.grant_s, policy.max_grant_s, self._max_grant_s)
        now = datetime.now(timezone.utc)
        grant = ApprovalGrant(
            id=f"gr_{tokens.token_hex(6)}",
            kind=KIND_SECRET,
            principal=request.agent,
            scope=request.secret,
            granted_by=answer.approver,
            granted_at=now.isoformat(timespec="seconds"),
            expires_at=(now + timedelta(seconds=seconds)).isoformat(timespec="seconds"),
        )
        await self._ledger.create_grant(grant)
        logger.info(
            "secret %s: %s may use it for %s (granted by %s)",
            request.secret, request.agent, humanize(seconds), answer.approver,
        )
        return grant

    # -- the ledger -----------------------------------------------------------

    async def _refuse(
        self, request: LeaseRequest, decision: str, started: float, request_id: str, *,
        approver: str = "", grant_id: str = "",
    ) -> LeaseResult:
        await self._record(
            request, decision, started, request_id, approver=approver, grant_id=grant_id
        )
        return LeaseResult(granted=False, decision=decision)

    async def _record(
        self, request: LeaseRequest, decision: str, started: float, request_id: str, *,
        approver: str = "", grant_id: str = "",
    ) -> None:
        # request.names rather than request.secret: a malformed multi-secret
        # request never gets this far, but the ledger must not raise if it did.
        name = request.names[0] if request.names else ""
        await self._ledger.record_decision(
            ApprovalAudit(
                id=f"au_{tokens.token_hex(8)}",
                at=_now(),
                kind=KIND_SECRET,
                principal=request.agent,
                scope=name,
                reason=request.reason[:_MAX_REASON],
                detail=command_line(request.command),
                decision=decision,
                approver=approver,
                grant_id=grant_id,
                request_id=request_id,
                duration_ms=int((time.monotonic() - started) * 1000),
            )
        )
        logger.info("secret %s for %s: %s", name, request.agent, decision)

    # -- operator surface -----------------------------------------------------

    async def unlock(self, material: UnlockMaterial) -> BackendStatus:
        state = await self._backend.unlock(material)
        logger.info(
            "secrets: unlock -> reachable=%s sealed=%s authenticated=%s",
            state.reachable, state.sealed, state.authenticated,
        )
        return state

    async def status(self) -> BackendStatus:
        return await self._backend.status()

    @property
    def backend(self) -> SecretBackend:
        """For the operator CLI, which reads and writes values directly. There
        is no path from an agent to this."""
        return self._backend


def _unusable(state: BackendStatus) -> str:
    if not state.reachable:
        return DECISION_BACKEND_ERROR
    return DECISION_SEALED if state.sealed else DECISION_LOCKED


def _verdict_line(request: LeaseRequest, answer: Approval) -> str:
    if not answer.allowed:
        verdict = "Denied"
    elif answer.grant_s > 0:
        verdict = f"Allowed for {humanize(answer.grant_s)}"
    else:
        verdict = "Allowed once"
    return _ANSWERED.format(verdict=verdict, agent=request.agent, secret=request.secret)
