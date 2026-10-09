"""The action gateway for external side effects (spec §6, §8, §9; T09, T16).

A worker asks permission for each side-effecting step before doing it and reports the outcome
afterwards. Holding a lease is not permission: the requested scope must be in the intersection
of the delegator's grant, the agent's grant, the contract's permission scope and the goal's
resource policy."""
from __future__ import annotations

from cas.domain import states
from cas.domain.errors import Conflict, NotFound
from cas.persistence.db import Tx
from cas.service import contracts, policy
from cas.service.core import Ctx, bump, emit, new_id


def get(tx: Tx, tenant_id: str, effect_id: str, lock: bool = False) -> dict:
    row = tx.one(f"SELECT * FROM external_effects WHERE tenant_id = %s AND id = %s{' FOR UPDATE' if lock else ''}",
                 (tenant_id, effect_id))
    if row is None:
        raise NotFound(f"effect '{effect_id}' not found")
    return row


def list_for_contract(tx: Tx, tenant_id: str, contract_id: str) -> list[dict]:
    return tx.all("SELECT * FROM external_effects WHERE tenant_id = %s AND contract_id = %s ORDER BY created_at",
                  (tenant_id, contract_id))


def _applied(e: dict) -> bool:
    return e["status"] == "succeeded" or (e["status"] == "recovered" and e["detail"].get("outcome") == "applied")


def authorize(ctx: Ctx, contract_id: str, lease_token: str, body: dict) -> dict:
    c = contracts.get(ctx.tx, ctx.tenant_id, contract_id, lock=True)
    contracts.require_lease(ctx, c, lease_token)
    if c["status"] != states.RUNNING:
        raise states.InvalidTransition(f"side effects can only be requested while the contract is running, not '{c['status']}'")
    scope = body["scope"]
    layers = policy.effective_scopes(ctx.tx, ctx.tenant_id, c, ctx.actor_id)
    if scope not in layers["effective"]:
        raise policy.deny(
            ctx, f"scope '{scope}' is not authorized for this contract and agent",
            action="effect.authorize", resource_type="contract", resource_id=c["id"], goal_id=c["goal_id"],
            escalate=True,
            details={"scope": scope, "step_key": body["step_key"],
                     "missing_in": policy.missing_layers(layers, scope)})
    prior = ctx.tx.all(
        "SELECT * FROM external_effects WHERE tenant_id = %s AND contract_id = %s AND step_key = %s ORDER BY created_at",
        (ctx.tenant_id, c["id"], body["step_key"]))
    for e in prior:
        if _applied(e):
            # Already done in an earlier attempt: tell the worker not to repeat it.
            return dict(e, already_applied=True)
        if e["status"] == "manual_recovery_required":
            raise Conflict(f"step '{e['step_key']}' needs manual recovery before it can run again",
                           code="MANUAL_RECOVERY_REQUIRED", details={"effect_id": e["id"]})
        if e["status"] == "authorized" and e["fencing_token"] == c["fencing_token"]:
            return dict(e, already_applied=False)
    row = ctx.tx.one(
        """INSERT INTO external_effects (tenant_id, id, contract_id, fencing_token, attempt_no, step_key, scope,
               idempotent, status, detail, actor_id)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'authorized',%s,%s) RETURNING *""",
        (ctx.tenant_id, new_id("effect"), c["id"], c["fencing_token"], c["attempt"], body["step_key"], scope,
         bool(body.get("idempotent", False)), body.get("detail") or {}, ctx.actor_id))
    emit(ctx, "cas.effect.authorized.v1", "effect", row["id"], row["version"], c["goal_id"],
         {"contract_id": c["id"], "step_key": row["step_key"], "scope": scope, "idempotent": row["idempotent"],
          "attempt": c["attempt"]})
    return dict(row, already_applied=False)


def report(ctx: Ctx, contract_id: str, effect_id: str, lease_token: str, succeeded: bool, detail: dict) -> dict:
    """Record what happened. A failed step that is not idempotent cannot be retried blindly."""
    c = contracts.get(ctx.tx, ctx.tenant_id, contract_id, lock=True)
    contracts.require_lease(ctx, c, lease_token)
    e = get(ctx.tx, ctx.tenant_id, effect_id, lock=True)
    if e["contract_id"] != c["id"]:
        raise NotFound(f"effect '{effect_id}' not found")
    if e["status"] != "authorized" or e["fencing_token"] != c["fencing_token"]:
        raise Conflict(f"this effect is already {e['status']}", code="EFFECT_ALREADY_REPORTED")
    status = "succeeded" if succeeded else ("failed" if e["idempotent"] else "manual_recovery_required")
    new = bump(ctx.tx, "external_effects", ctx.tenant_id, e["id"], status=status, detail={**e["detail"], **detail})
    emit(ctx, f"cas.effect.{status}.v1", "effect", e["id"], new["version"], c["goal_id"],
         {"contract_id": c["id"], "step_key": e["step_key"], "scope": e["scope"], "idempotent": e["idempotent"]})
    return new


def recover(ctx: Ctx, effect_id: str, outcome: str, note: str) -> dict:
    """A human records what actually happened to a side effect that needed manual recovery."""
    e = get(ctx.tx, ctx.tenant_id, effect_id, lock=True)
    c = contracts.get(ctx.tx, ctx.tenant_id, e["contract_id"], lock=True)
    if not (ctx.is_human and contracts._has_authority(ctx, c)):
        raise policy.deny(ctx, "only a human requester, goal owner or admin may record a manual recovery",
                          action="effect.recover", resource_type="contract", resource_id=c["id"], goal_id=c["goal_id"])
    if e["status"] != "manual_recovery_required":
        raise Conflict(f"this effect is {e['status']}; it does not need recovery", code="EFFECT_NOT_RECOVERABLE")
    new = bump(ctx.tx, "external_effects", ctx.tenant_id, e["id"], status="recovered",
               detail={**e["detail"], "outcome": outcome, "recovery_note": note, "recovered_by": ctx.actor_id})
    emit(ctx, "cas.effect.recovered.v1", "effect", e["id"], new["version"], c["goal_id"],
         {"contract_id": c["id"], "step_key": e["step_key"], "outcome": outcome, "note": note})
    return new
