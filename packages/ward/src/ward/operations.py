"""What an operator can do, once the door has established they are one.

Everything here is behind the operator certificate. It is the half of the system
an agent has no route to at all: storing a value, saying who may reach it,
reading what has been asked for. Keeping it on this side of the door — rather
than on the engine's loopback, where the agents' shells also live — is the
reason the broker moved out of the engine in the first place.

The shapes are plain dictionaries: this is a wire contract for one CLI, not a
public API, and a dataclass per route would only be a second place to change.
"""

import logging
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any

from crucible.store.base import ApprovalStore
from ward.decisions import KIND_SECRET
from ward.ports import SecretBackend, SecretBackendError
from ward.store import SecretPolicyRecord, SecretPolicyStore

logger = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Operations:
    """The operator's view of the store: values, policies, windows, ledger."""

    def __init__(
        self, backend: SecretBackend, policies: SecretPolicyStore, ledger: ApprovalStore
    ) -> None:
        self._backend = backend
        self._policies = policies
        self._ledger = ledger

    # -- values ---------------------------------------------------------------

    async def list_secrets(self) -> dict[str, Any]:
        """Names and their policies. Never values, on any path: there is no verb
        anywhere in this system that reads a secret out to a human."""
        try:
            stored = await self._backend.names()
        except SecretBackendError as exc:
            return {"error": str(exc)}
        policies = {p.name: _policy_json(p) for p in await self._policies.list_policies()}
        return {
            "secrets": [
                {"name": name, "stored": name in stored, "policy": policies.get(name)}
                for name in sorted(set(stored) | set(policies))
            ]
        }

    async def put_secret(self, name: str, fields: Mapping[str, str]) -> dict[str, Any]:
        await self._backend.write(name, dict(fields))
        logger.info("stored %s (%s)", name, ", ".join(sorted(fields)))
        return {"stored": name, "fields": sorted(fields)}

    async def delete_secret(self, name: str) -> dict[str, Any]:
        await self._backend.delete(name)
        await self._policies.delete_policy(name)
        # The windows go with it, or an agent keeps reaching something whose
        # permission has just been deleted.
        closed = await self._ledger.revoke_scope(KIND_SECRET, name, now=_now())
        logger.info("removed %s (%d window(s) closed)", name, closed)
        return {"removed": name, "windows_closed": closed}

    async def rotate_credential(self) -> dict[str, Any]:
        """Replace the broker's own credential.

        The running process keeps serving on the token it already has, so this
        does not interrupt anything — but the next unlock needs the new secret
        id, and the caller is the only one who will ever see it.
        """
        secret_id = await self._backend.rotate()
        logger.info("the broker's credential was replaced by the operator")
        return {"secret_id": secret_id}

    # -- policies -------------------------------------------------------------

    async def list_policies(self) -> dict[str, Any]:
        return {"policies": [_policy_json(p) for p in await self._policies.list_policies()]}

    async def put_policy(self, name: str, body: Mapping[str, Any]) -> dict[str, Any]:
        existing = await self._policies.get_policy(name)
        now = _now()
        policy = SecretPolicyRecord(
            name=name,
            approval=str(body.get("approval") or "always"),
            max_grant_s=int(body.get("max_grant_s") or 0),
            subjects=str(body.get("subjects") or ""),
            description=str(body.get("description") or ""),
            created_at=existing.created_at if existing else now,
            updated_at=now,
        )
        await self._policies.put_policy(policy)
        logger.info("policy for %s: %s, for: %s", name, policy.approval, policy.subjects)
        return {"policy": _policy_json(policy)}

    # -- windows and the ledger -----------------------------------------------

    async def list_grants(self, *, include_dead: bool = False) -> dict[str, Any]:
        grants = await self._ledger.list_grants(
            now=_now(), kind=KIND_SECRET, include_dead=include_dead
        )
        return {
            "grants": [
                {
                    "id": g.id, "agent": g.principal, "secret": g.scope,
                    "granted_by": g.granted_by, "granted_at": g.granted_at,
                    "expires_at": g.expires_at, "revoked_at": g.revoked_at,
                }
                for g in grants
            ]
        }

    async def revoke_grant(self, grant_id: str) -> dict[str, Any]:
        return {"closed": await self._ledger.revoke_grant(grant_id, now=_now())}

    async def list_audit(
        self, *, limit: int = 50, agent: str = "", secret: str = "", kind: str = ""
    ) -> dict[str, Any]:
        """The ledger, of one kind or of all of them.

        Both kinds live in one table and both answer "what happened here": a
        request for a credential, and an operator acting from chat. Defaulting
        to all of them is deliberate — an audit row nobody's reader shows is not
        an audit row, and the operator ones were invisible while this filtered.
        """
        rows = await self._ledger.list_audit(
            limit=limit, kind=kind, principal=agent, scope=secret
        )
        return {
            "audit": [
                {
                    "at": r.at, "agent": r.principal, "secret": r.scope,
                    "reason": r.reason, "detail": r.detail, "decision": r.decision,
                    "approver": r.approver, "request_id": r.request_id,
                    "kind": r.kind,
                }
                for r in rows
            ]
        }


def _policy_json(policy: SecretPolicyRecord) -> dict[str, Any]:
    return {
        "name": policy.name,
        "approval": policy.approval,
        "max_grant_s": policy.max_grant_s,
        "subjects": policy.subjects,
        "description": policy.description,
    }
