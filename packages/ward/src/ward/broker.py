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
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from crucible.approvals import (
    Approval,
    PendingApprovals,
    approval_actions,
    humanize,
    windows,
)
from crucible.approvals.card import command_line
from crucible.ports.chat.admin import ChatAdmin
from crucible.ports.chat.types import ConversationRef
from crucible.store.base import (
    DECISION_APPROVED_GRANT,
    DECISION_APPROVED_ONCE,
    DECISION_DENIED,
    DECISION_NO_APPROVER,
    DECISION_REUSED_GRANT,
    DECISION_TIMEOUT,
    ApprovalAudit,
    ApprovalGrant,
    ApprovalStore,
)
from ward.approvers import Approvers
from ward.card import approval_text, notice_text, verdict_text
from ward.decisions import (
    DECISION_AUTO,
    DECISION_AUTO_COMMAND,
    DECISION_BACKEND_ERROR,
    DECISION_LOCKED,
    DECISION_NO_POLICY,
    DECISION_NOT_PERMITTED,
    DECISION_NOT_REACHED,
    DECISION_SEALED,
    KIND_SECRET,
)
from ward.policy import ASK, REFUSE, ceiling, evaluate
from ward.ports import (
    AgentPosters,
    BackendStatus,
    LeaseRequest,
    LeaseResult,
    SecretBackend,
    SecretBackendError,
    UnlockMaterial,
)
from ward.store import SecretPolicyRecord, SecretPolicyStore

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Folded:
    """A notice already posted, and how many grants it now stands for."""

    post_id: str
    repeats: int
    until: float

# The card is rewritten once it has been answered, so a second click finds no
# buttons and the ledger cannot disagree with what the message says happened.
# What it is rewritten TO is built by `verdict_text` — it keeps the command,
# which is what makes the message readable as history rather than as a receipt.
_EXPIRED = "Nobody answered in time"

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
        approvals: PendingApprovals,
        approvers: Approvers,
        *,
        approval_channel: str = "",
        approval_timeout_s: float = 120.0,
        max_grant_s: int = 3600,
        notice_fold_s: float = 900.0,
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
        self._approvers = approvers
        self._approval_channel = approval_channel
        self._timeout = approval_timeout_s
        self._max_grant_s = max_grant_s
        self._callback_url = callback_url
        self._fold_s = notice_fold_s
        # What has already been said, so a repeat edits its own message rather
        # than adding one. In memory: a notice nobody is looking at any more is
        # not worth a row in a database.
        self._notices: dict[tuple[str, tuple[str, ...]], Folded] = {}

    # -- the decision ---------------------------------------------------------

    async def lease(self, request: LeaseRequest) -> LeaseResult:
        """Serve every secret the request names, or none of them.

        All-or-nothing is the rule. A caller that got half its environment would
        run its command anyway and fail somewhere less obvious, and a human who
        approved a set should not have part of it quietly dropped.
        """
        started = time.monotonic()
        # One id per invocation: a request leaves a ledger row per secret, and an
        # operator reading them needs to see which belong to the same click.
        request_id = f"rq_{tokens.token_hex(6)}"
        names = request.names  # ValueError -> malformed, not a refusal
        policies = {name: await self._policies.get_policy(name) for name in names}
        verdicts = {
            name: evaluate(policies[name], request.agent, request.command)
            for name in names
        }

        refusal = next(
            (v.decision for v in verdicts.values() if v.outcome == REFUSE), ""
        )
        if refusal:
            return await self._refuse(request, verdicts, refusal, started, request_id)

        state = await self._backend.status()
        if not state.usable:
            return await self._refuse(request, verdicts, _unusable(state), started, request_id)

        # Which of them still needs a human: the ones whose policy asks and that
        # no open window already covers.
        asking = [name for name in names if verdicts[name].outcome == ASK]
        covered = {
            name: await self._ledger.live_grant(KIND_SECRET, request.agent, name, now=_now())
            for name in asking
        }
        uncovered = [name for name in asking if covered[name] is None]

        decisions = {
            name: (
                DECISION_REUSED_GRANT if covered.get(name) is not None
                else verdicts[name].decision
            )
            for name in names
        }
        grant_ids = {name: (g.id if g else "") for name, g in covered.items()}
        approver = ""

        if uncovered:
            # The most restrictive ceiling in the set governs the whole card: a
            # basket cannot be left open longer than its tightest member allows.
            longest = min(
                ceiling(policies[name], deployment_s=self._max_grant_s)
                for name in uncovered
            )
            answer = await self._ask(request, longest)
            if answer is None:
                return await self._refuse(
                    request, verdicts, DECISION_NO_APPROVER, started, request_id
                )
            approver = answer.approver
            if not answer.allowed:
                refused = DECISION_TIMEOUT if answer.timed_out else DECISION_DENIED
                return await self._refuse(
                    request, verdicts, refused, started, request_id, approver=approver
                )
            # A rule-grant inside a request a human answered was NOT unwatched:
            # the card listed every secret, and they said yes to the basket. The
            # decision names what authorized it, so here that is the human — the
            # rule stays visible in the row's reason.
            answered = [
                name for name in names
                if verdicts[name].rule or name in uncovered
            ]
            for name in answered:
                if answer.grant_s > 0 and name in uncovered:
                    grant_ids[name] = await self._open_window(
                        request.agent, name, policies[name], answer
                    )
                    decisions[name] = DECISION_APPROVED_GRANT
                else:
                    decisions[name] = DECISION_APPROVED_ONCE

        try:
            values = {
                env_name: await self._backend.read(ref) for env_name, ref in request.bindings
            }
        except SecretBackendError as exc:
            failure = DECISION_SEALED if exc.sealed else DECISION_BACKEND_ERROR
            logger.warning("secrets for %s: %s", request.agent, exc)
            return await self._refuse(
                request, verdicts, failure, started, request_id,
                approver=approver, grant_ids=grant_ids,
            )

        for name in names:
            await self._record(
                request, name, decisions[name], started, request_id,
                approver=approver, grant_id=grant_ids.get(name, ""),
                rule=verdicts[name].rule,
            )
        if not uncovered:
            # Nobody was asked, so nobody knows this happened unless it is said.
            # Only for the rules: `approval: never` is a deliberate silence, and
            # a window was opened by a human who has already seen a card.
            await self._notify(request, verdicts)
        # The request's own verdict, for a caller that logs one line: the least
        # automatic thing that happened to any of its secrets.
        return LeaseResult(granted=True, decision=_summarize(decisions), values=values)

    # -- asking ---------------------------------------------------------------

    async def _ask(self, request: LeaseRequest, ceiling_s: int) -> Approval | None:
        """Put the request in front of a human. None when there was nobody to
        put it in front of — a missing approver is a configuration problem, and
        must not read as a refusal in the ledger."""
        approvers = await self._approvers.ids()
        poster = self._presence.poster(request.agent)
        if not approvers or poster is None:
            logger.warning(
                "secrets %s for %s: nobody to ask (approvers=%d, poster=%s)",
                ", ".join(request.names), request.agent, len(approvers), poster is not None,
            )
            return None
        channel = await self._approval_conversation(request.agent, approvers)
        if not channel:
            return None

        token = tokens.token_hex(16)
        offers = windows(ceiling_s=ceiling_s)
        future = self._approvals.register(
            token, kind=KIND_SECRET, principal=request.agent,
            scopes=request.names, approvers=approvers,
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
                approval_actions(token, offers=offers),
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
            await self._rewrite(poster, post_id, _verdict_line(request, None))
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
        self, agent: str, name: str, policy: SecretPolicyRecord | None, answer: Approval
    ) -> str:
        """Record the window a human opened, capped by the policy and by the
        deployment — the ladder is filtered before it is shown, and capped again
        here so a hand-made callback cannot ask for a year."""
        seconds = min(
            answer.grant_s, ceiling(policy, deployment_s=self._max_grant_s)
        )
        now = datetime.now(timezone.utc)
        grant = ApprovalGrant(
            id=f"gr_{tokens.token_hex(6)}",
            kind=KIND_SECRET,
            principal=agent,
            scope=name,
            granted_by=answer.approver,
            granted_at=now.isoformat(timespec="seconds"),
            expires_at=(now + timedelta(seconds=seconds)).isoformat(timespec="seconds"),
        )
        await self._ledger.create_grant(grant)
        logger.info(
            "secret %s: %s may use it for %s (granted by %s)",
            name, agent, humanize(seconds), answer.approver,
        )
        return grant.id

    # -- telling somebody -----------------------------------------------------

    async def _notify(self, request: LeaseRequest, verdicts: dict) -> None:
        """Say that a secret was taken without anyone being asked.

        Detection, not control: by the time this arrives the value is out. What
        it buys is that an automatic grant is not also an invisible one — and
        the honest way to describe a rule is "quieter", not "unwatched".

        Folded rather than repeated. A task on a schedule would otherwise turn
        the approver's direct messages into a feed nobody reads, and a notice
        nobody reads is the same as no notice.
        """
        rules = sorted({v.rule for v in verdicts.values() if v.rule})
        if not rules:
            return  # `approval: never` is a deliberate silence, not this
        approvers = await self._approvers.ids()
        poster = self._presence.poster(request.agent)
        if not approvers or poster is None:
            return
        channel = await self._approval_conversation(request.agent, approvers)
        if not channel:
            return

        key = (request.agent, request.names)
        now = time.monotonic()
        seen = self._notices.get(key)
        repeats = seen.repeats + 1 if seen and now < seen.until else 1
        text = notice_text(
            request.agent,
            references=request.references,
            command=request.command,
            rules=tuple(rules),
            repeats=repeats,
        )
        ref = ConversationRef(
            channel_id=channel, conversation_id=channel, message_id=channel
        )
        try:
            if seen is not None and now < seen.until:
                await poster.retract(seen.post_id, text)
                self._notices[key] = Folded(seen.post_id, repeats, seen.until)
                return
            post_id = await poster.post_actions(
                ref, text, [], callback_url=self._callback_url
            )
        except Exception:
            logger.warning("could not report an automatic grant", exc_info=True)
            return
        self._notices[key] = Folded(str(post_id or ""), repeats, now + self._fold_s)

    # -- the ledger -----------------------------------------------------------

    async def _refuse(
        self, request: LeaseRequest, verdicts: dict, governing: str, started: float,
        request_id: str, *, approver: str = "", grant_ids: dict | None = None,
    ) -> LeaseResult:
        """Refuse the whole request, and say per secret what happened to it.

        A secret that was individually fine but never got decided — because a
        sibling failed first — is recorded as such rather than being tarred with
        the refusal it did not earn.
        """
        # An authorization refusal belongs to the secret that earned it; the
        # others were simply never decided. Everything else — locked, sealed,
        # denied, nobody answered — applies to the request as a whole.
        per_secret = governing in (DECISION_NO_POLICY, DECISION_NOT_PERMITTED)
        for name in request.names:
            verdict = verdicts.get(name)
            if verdict is not None and verdict.outcome == REFUSE:
                decision = verdict.decision
            elif per_secret:
                decision = DECISION_NOT_REACHED
            else:
                decision = governing
            await self._record(
                request, name, decision, started, request_id,
                approver=approver, grant_id=(grant_ids or {}).get(name, ""),
            )
        return LeaseResult(granted=False, decision=governing)

    async def _record(
        self, request: LeaseRequest, name: str, decision: str, started: float,
        request_id: str, *, approver: str = "", grant_id: str = "", rule: str = "",
    ) -> None:
        await self._ledger.record_decision(
            ApprovalAudit(
                id=f"au_{tokens.token_hex(8)}",
                at=_now(),
                kind=KIND_SECRET,
                principal=request.agent,
                scope=name,
                # The rule goes in front of the caller's own words: when nobody
                # was asked, "why was this handed over" is answered by the rule
                # and by nothing else.
                reason=(
                    f"rule: {rule} — {request.reason[:_MAX_REASON]}"
                    if rule else request.reason[:_MAX_REASON]
                ),
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


def _summarize(decisions: dict[str, str]) -> str:
    """One word for a request that served several secrets — the least automatic
    thing that happened to any of them, so a caller logging a single line is not
    told "auto" about a basket a human had to approve."""
    order = (
        DECISION_APPROVED_ONCE, DECISION_APPROVED_GRANT,
        DECISION_REUSED_GRANT, DECISION_AUTO_COMMAND, DECISION_AUTO,
    )
    for decision in order:
        if decision in decisions.values():
            return decision
    return DECISION_AUTO


def _verdict_line(request: LeaseRequest, answer: Approval | None) -> str:
    """The answered card. ``None`` means nobody answered in time."""
    if answer is None:
        verdict = f"⌛ {_EXPIRED}, so the request was refused"
    elif not answer.allowed:
        verdict = "Denied"
    elif answer.grant_s > 0:
        verdict = f"Allowed for {humanize(answer.grant_s)}"
    else:
        verdict = "Allowed once"
    return verdict_text(
        verdict, request.agent, references=request.references, command=request.command
    )
