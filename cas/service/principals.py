"""Tenants, principals, API tokens, agent capabilities and policy grants."""
from __future__ import annotations

import hashlib
import secrets

from cas.domain.errors import Conflict, NotFound, Unprocessable
from cas.persistence.db import Tx
from cas.service import policy
from cas.service.core import RECONCILER_ID, Ctx, bump, emit, new_id

ATTENTION_LEVELS = ("ambient", "digest", "decision", "interrupt")


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def issue_token(tx: Tx, tenant_id: str, principal_id: str) -> str:
    token = "cas_" + secrets.token_urlsafe(32)
    tx.execute("INSERT INTO api_tokens (token_hash, tenant_id, principal_id) VALUES (%s,%s,%s)",
               (hash_token(token), tenant_id, principal_id))
    return token


def authenticate(tx: Tx, token: str) -> dict | None:
    return tx.one(
        """SELECT p.* FROM api_tokens t JOIN principals p
             ON p.tenant_id = t.tenant_id AND p.id = t.principal_id
           WHERE t.token_hash = %s AND t.revoked_at IS NULL AND p.status = 'active'""",
        (hash_token(token),),
    )


def _insert_principal(tx: Tx, tenant_id: str, kind: str, display_name: str, issuer: str,
                      external_subject: str, principal_id: str | None = None,
                      attention_preference: str = "decision") -> dict:
    pid = principal_id or new_id({"human": "human", "agent": "agent", "service": "service"}[kind])
    if tx.one("SELECT 1 FROM principals WHERE tenant_id = %s AND (id = %s OR (issuer = %s AND external_subject = %s))",
              (tenant_id, pid, issuer, external_subject)):
        raise Conflict("a principal with this id or issuer/subject already exists", code="PRINCIPAL_EXISTS")
    return tx.one(
        """INSERT INTO principals (tenant_id, id, kind, display_name, issuer, external_subject, attention_preference)
           VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING *""",
        (tenant_id, pid, kind, display_name, issuer, external_subject, attention_preference),
    )


def bootstrap_tenant(tx: Tx, tenant_id: str, name: str, admin_name: str = "Administrator",
                     admin_id: str | None = None) -> dict:
    """Create a tenant with its first human admin and the reconciler service principal.
    This is the only path that creates an admin without an existing admin."""
    if tx.one("SELECT 1 FROM tenants WHERE id = %s", (tenant_id,)):
        raise Conflict(f"tenant '{tenant_id}' already exists", code="TENANT_EXISTS")
    tx.execute("INSERT INTO tenants (id, name) VALUES (%s,%s)", (tenant_id, name))
    admin = _insert_principal(tx, tenant_id, "human", admin_name, "cas:bootstrap", "admin", admin_id)
    _insert_principal(tx, tenant_id, "service", "CAS reconciler", "cas:internal", "reconciler", RECONCILER_ID)
    for role in ("admin", "auditor"):
        tx.execute(
            """INSERT INTO policy_grants (tenant_id, id, principal_id, role, resource_type, granted_by)
               VALUES (%s,%s,%s,%s,'tenant','cas:bootstrap')""",
            (tenant_id, new_id("grant"), admin["id"], role))
    tx.execute(
        """INSERT INTO policy_grants (tenant_id, id, principal_id, role, resource_type, granted_by)
           VALUES (%s,%s,%s,'validator','tenant','cas:bootstrap')""",
        (tenant_id, new_id("grant"), RECONCILER_ID))
    return {"tenant_id": tenant_id, "admin": admin, "token": issue_token(tx, tenant_id, admin["id"])}


def get_principal(tx: Tx, tenant_id: str, principal_id: str) -> dict:
    row = tx.one("SELECT * FROM principals WHERE tenant_id = %s AND id = %s", (tenant_id, principal_id))
    if row is None:
        raise NotFound(f"principal '{principal_id}' not found")
    return row


def require_principal(tx: Tx, tenant_id: str, principal_id: str, field: str, kind: str | None = None) -> dict:
    """Resolve a principal referenced in a request body; unknown or foreign ids are a 422."""
    row = tx.one("SELECT * FROM principals WHERE tenant_id = %s AND id = %s AND status = 'active'",
                 (tenant_id, principal_id))
    if row is None:
        raise Unprocessable(f"{field} '{principal_id}' is not an active principal in this tenant",
                            code="UNKNOWN_PRINCIPAL", details={"field": field})
    if kind and row["kind"] != kind:
        raise Unprocessable(f"{field} must be a {kind} principal", code="WRONG_PRINCIPAL_KIND",
                            details={"field": field})
    return row


def create_principal(ctx: Ctx, body: dict) -> dict:
    policy.require_admin(ctx, "principal.create")
    kind = body["kind"]
    row = _insert_principal(ctx.tx, ctx.tenant_id, kind, body["display_name"],
                            body.get("issuer") or "cas:local", body["external_subject"], body.get("id"),
                            body.get("attention_preference") or "decision")
    emit(ctx, "cas.principal.registered.v1", "principal", row["id"], row["version"], None, {"kind": kind})
    out = dict(row)
    if body.get("issue_token", True):
        out["token"] = issue_token(ctx.tx, ctx.tenant_id, row["id"])
    return out


def register_agent(ctx: Ctx, body: dict) -> dict:
    """Admin-controlled agent registration (spec §5). Capabilities are scheduling hints only."""
    policy.require_admin(ctx, "agent.register")
    row = _insert_principal(ctx.tx, ctx.tenant_id, "agent", body["display_name"],
                            body.get("issuer") or "cas:local", body["external_subject"], body.get("id"))
    for cap in body.get("capabilities", []):
        ctx.tx.execute(
            """INSERT INTO agent_capabilities (tenant_id, id, agent_id, capability_key, description,
                   input_schema_ref, output_schema_ref, adapter_type, adapter_config_ref, limits)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (ctx.tenant_id, new_id("cap"), row["id"], cap["capability_key"], cap.get("description", ""),
             cap.get("input_schema_ref"), cap.get("output_schema_ref"), cap.get("adapter_type", "pull"),
             cap.get("adapter_config_ref"), cap.get("limits") or {}))
    emit(ctx, "cas.principal.registered.v1", "principal", row["id"], row["version"], None, {"kind": "agent"})
    out = get_agent(ctx.tx, ctx.tenant_id, row["id"])
    if body.get("issue_token", True):
        out["token"] = issue_token(ctx.tx, ctx.tenant_id, row["id"])
    return out


def get_agent(tx: Tx, tenant_id: str, agent_id: str) -> dict:
    row = tx.one("SELECT * FROM principals WHERE tenant_id = %s AND id = %s AND kind = 'agent'",
                 (tenant_id, agent_id))
    if row is None:
        raise NotFound(f"agent '{agent_id}' not found")
    out = dict(row)
    out["capabilities"] = tx.all(
        """SELECT capability_key, description, input_schema_ref, output_schema_ref, adapter_type,
                  adapter_config_ref, limits
           FROM agent_capabilities WHERE tenant_id = %s AND agent_id = %s ORDER BY capability_key""",
        (tenant_id, agent_id))
    return out


def list_agents(tx: Tx, tenant_id: str, capability: str | None, cursor: str | None, limit: int) -> dict:
    rows = tx.all(
        """SELECT p.id FROM principals p
           WHERE p.tenant_id = %(t)s AND p.kind = 'agent' AND p.id > %(cursor)s
             AND (%(cap)s::text IS NULL OR EXISTS (SELECT 1 FROM agent_capabilities c
                  WHERE c.tenant_id = p.tenant_id AND c.agent_id = p.id AND c.capability_key = %(cap)s))
           ORDER BY p.id LIMIT %(limit)s""",
        {"t": tenant_id, "cap": capability, "cursor": cursor or "", "limit": limit + 1})
    ids = [r["id"] for r in rows]
    return {"items": [get_agent(tx, tenant_id, i) for i in ids[:limit]],
            "next_cursor": ids[limit - 1] if len(ids) > limit else None}


def list_principals(tx: Tx, tenant_id: str) -> list[dict]:
    return tx.all("SELECT * FROM principals WHERE tenant_id = %s ORDER BY kind, display_name", (tenant_id,))


def create_grant(ctx: Ctx, body: dict) -> dict:
    """Grant a role (and optionally scopes). Only a human admin may grant; no principal that is
    not human may grant to itself, and human-only roles cannot go to agents (spec §8)."""
    policy.require_admin(ctx, "grant.create", "principal", body.get("principal_id"))
    grantee = require_principal(ctx.tx, ctx.tenant_id, body["principal_id"], "principal_id")
    role = body["role"]
    if role not in policy.ROLES:
        raise Unprocessable(f"unknown role '{role}'", code="UNKNOWN_ROLE")
    if role in policy.HUMAN_ONLY_ROLES and grantee["kind"] != "human":
        raise Unprocessable(f"role '{role}' can only be granted to a human principal", code="ROLE_REQUIRES_HUMAN")
    goal_id = body.get("goal_id")
    if goal_id and not ctx.tx.one("SELECT 1 FROM goals WHERE tenant_id = %s AND id = %s", (ctx.tenant_id, goal_id)):
        raise Unprocessable(f"goal '{goal_id}' not found", code="UNKNOWN_GOAL")
    row = ctx.tx.one(
        """INSERT INTO policy_grants (tenant_id, id, principal_id, role, resource_type, resource_id, scopes, granted_by)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *""",
        (ctx.tenant_id, new_id("grant"), grantee["id"], role, "goal" if goal_id else "tenant", goal_id,
         sorted(set(body.get("scopes") or [])), ctx.actor_id))
    emit(ctx, "cas.grant.created.v1", "grant", row["id"], row["version"], goal_id,
         {"principal_id": grantee["id"], "role": role, "scopes": row["scopes"]})
    return row


def revoke_grant(ctx: Ctx, grant_id: str) -> dict:
    policy.require_admin(ctx, "grant.revoke", "grant", grant_id)
    row = ctx.tx.one("SELECT * FROM policy_grants WHERE tenant_id = %s AND id = %s FOR UPDATE",
                     (ctx.tenant_id, grant_id))
    if row is None:
        raise NotFound(f"grant '{grant_id}' not found")
    if row["revoked_at"] is None:
        ctx.tx.execute("UPDATE policy_grants SET revoked_at = now() WHERE tenant_id = %s AND id = %s",
                       (ctx.tenant_id, grant_id))
        row = bump(ctx.tx, "policy_grants", ctx.tenant_id, grant_id)
        emit(ctx, "cas.grant.revoked.v1", "grant", row["id"], row["version"], row["resource_id"],
             {"principal_id": row["principal_id"], "role": row["role"]})
    return row


def list_grants(tx: Tx, tenant_id: str, principal_id: str | None = None) -> list[dict]:
    return tx.all(
        """SELECT * FROM policy_grants WHERE tenant_id = %s AND revoked_at IS NULL
             AND (%s::text IS NULL OR principal_id = %s) ORDER BY created_at""",
        (tenant_id, principal_id, principal_id))


def set_resource_policy(ctx: Ctx, goal_id: str, allowed_scopes: list[str]) -> dict:
    policy.require_admin(ctx, "resource_policy.set", "goal", goal_id)
    if not ctx.tx.one("SELECT 1 FROM goals WHERE tenant_id = %s AND id = %s", (ctx.tenant_id, goal_id)):
        raise NotFound(f"goal '{goal_id}' not found")
    row = ctx.tx.one(
        """INSERT INTO resource_policies (tenant_id, goal_id, allowed_scopes, updated_by)
           VALUES (%s,%s,%s,%s)
           ON CONFLICT (tenant_id, goal_id) DO UPDATE
             SET allowed_scopes = EXCLUDED.allowed_scopes, updated_by = EXCLUDED.updated_by,
                 updated_at = now(), version = resource_policies.version + 1
           RETURNING *""",
        (ctx.tenant_id, goal_id, sorted(set(allowed_scopes)), ctx.actor_id))
    emit(ctx, "cas.resource_policy.set.v1", "resource_policy", goal_id, row["version"], goal_id,
         {"allowed_scopes": row["allowed_scopes"]})
    return row


def set_attention_preference(ctx: Ctx, level: str) -> dict:
    if level not in ATTENTION_LEVELS:
        raise Unprocessable(f"unknown attention level '{level}'", code="UNKNOWN_ATTENTION_LEVEL")
    return bump(ctx.tx, "principals", ctx.tenant_id, ctx.actor_id, attention_preference=level)
