"""Authority policy (spec §8): role grants, scope grants and the delegation intersection.

Default deny: a principal can do only what an unrevoked grant allows. Goal preferences never
feed into these checks, so nothing a goal says can widen what an actor may do.
"""
from __future__ import annotations

from cas.domain.errors import PolicyDenied
from cas.persistence.db import Tx
from cas.service.core import Ctx

ROLES = ("goal_owner", "task_requester", "task_owner", "validator", "decision_owner", "admin", "auditor")
HUMAN_ONLY_ROLES = frozenset({"admin", "goal_owner", "auditor"})


def _grants(tx: Tx, tenant_id: str, principal_id: str, goal_id: str | None) -> list[dict]:
    return tx.all(
        """SELECT role, scopes FROM policy_grants
           WHERE tenant_id = %s AND principal_id = %s AND revoked_at IS NULL
             AND (resource_type = 'tenant' OR (resource_type = 'goal' AND resource_id = %s))""",
        (tenant_id, principal_id, goal_id),
    )


def roles(tx: Tx, tenant_id: str, principal_id: str, goal_id: str | None = None) -> set[str]:
    return {g["role"] for g in _grants(tx, tenant_id, principal_id, goal_id)}


def has_role(tx: Tx, tenant_id: str, principal_id: str, role: str, goal_id: str | None = None) -> bool:
    return role in roles(tx, tenant_id, principal_id, goal_id)


def scopes(tx: Tx, tenant_id: str, principal_id: str, goal_id: str | None) -> set[str]:
    out: set[str] = set()
    for g in _grants(tx, tenant_id, principal_id, goal_id):
        out.update(g["scopes"] or [])
    return out


def resource_scopes(tx: Tx, tenant_id: str, goal_id: str) -> set[str]:
    row = tx.one("SELECT allowed_scopes FROM resource_policies WHERE tenant_id = %s AND goal_id = %s",
                 (tenant_id, goal_id))
    return set(row["allowed_scopes"]) if row else set()


def is_admin(ctx: Ctx) -> bool:
    return ctx.is_human and has_role(ctx.tx, ctx.tenant_id, ctx.actor_id, "admin")


def deny(ctx: Ctx, message: str, *, action: str, resource_type: str, resource_id: str | None = None,
         goal_id: str | None = None, escalate: bool = False, details: dict | None = None) -> PolicyDenied:
    return PolicyDenied(message, action=action, resource_type=resource_type, resource_id=resource_id,
                        goal_id=goal_id, escalate=escalate, details=details)


def require_admin(ctx: Ctx, action: str, resource_type: str = "tenant", resource_id: str | None = None) -> None:
    if not is_admin(ctx):
        raise deny(ctx, "this action requires a human principal with the admin role",
                   action=action, resource_type=resource_type, resource_id=resource_id)


def require_role(ctx: Ctx, role: str, goal_id: str | None, *, action: str, resource_type: str,
                 resource_id: str | None = None) -> None:
    if not has_role(ctx.tx, ctx.tenant_id, ctx.actor_id, role, goal_id):
        raise deny(ctx, f"this action requires the '{role}' role on the goal or the tenant",
                   action=action, resource_type=resource_type, resource_id=resource_id, goal_id=goal_id)


def effective_scopes(tx: Tx, tenant_id: str, contract: dict, agent_id: str) -> dict[str, set[str]]:
    """The four layers of the delegation intersection and their intersection (never the union)."""
    layers = {
        "delegator": scopes(tx, tenant_id, contract["requester_id"], contract["goal_id"]),
        "agent": scopes(tx, tenant_id, agent_id, contract["goal_id"]),
        "task": set(contract["permission_scope"] or []),
        "resource": resource_scopes(tx, tenant_id, contract["goal_id"]),
    }
    layers["effective"] = layers["delegator"] & layers["agent"] & layers["task"] & layers["resource"]
    return layers


def missing_layers(layers: dict[str, set[str]], scope: str) -> list[str]:
    return [name for name in ("delegator", "agent", "task", "resource") if scope not in layers[name]]
