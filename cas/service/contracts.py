"""Coordination contract lifecycle (spec §3, §4, §6): creation, readiness and input pinning,
leases and fencing, submission, validation, retries, and invalidation propagation."""
from __future__ import annotations

import json
import random
import secrets

from cas.config import settings
from cas.domain import schemas, states
from cas.domain.errors import Conflict, NotFound, QuotaExceeded, Unprocessable
from cas.persistence.db import Tx
from cas.service import artifacts, policy
from cas.service.core import Ctx, bump, check_version, emit, new_id
from cas.service.principals import require_principal

REVISABLE_FIELDS = ("title", "objective", "owner_id", "input_refs", "deliverable_spec", "acceptance_checks",
                    "permission_scope", "priority", "deadline", "max_attempts", "escalation_policy")
SCHEMA_FIELDS = ("goal_id", "title", "objective", "requester_id", "owner_id", "parent_contract_id", "input_refs",
                 "deliverable_spec", "acceptance_checks", "permission_scope", "priority", "deadline",
                 "max_attempts", "escalation_policy")
# Blocks the reconciler may lift on its own once the cause is gone. Others need an explicit revalidate.
AUTO_UNBLOCK = frozenset({"dependency_unmet", "decision_hold"})
AUTO_RETRY_REASONS = frozenset({"lease_expired", "execution_error_retryable"})
DEFAULT_MAX_CONCURRENT = 4


# ---------------------------------------------------------------- reads

def get(tx: Tx, tenant_id: str, contract_id: str, lock: bool = False) -> dict:
    row = tx.one(f"SELECT * FROM contracts WHERE tenant_id = %s AND id = %s{' FOR UPDATE' if lock else ''}",
                 (tenant_id, contract_id))
    if row is None:
        raise NotFound(f"contract '{contract_id}' not found")
    return row


def dependency_ids(tx: Tx, tenant_id: str, contract_id: str) -> list[str]:
    return [r["upstream_id"] for r in tx.all(
        "SELECT upstream_id FROM contract_dependencies WHERE tenant_id = %s AND downstream_id = %s ORDER BY created_at, upstream_id",
        (tenant_id, contract_id))]


def active_lease(tx: Tx, tenant_id: str, contract_id: str) -> dict | None:
    return tx.one(
        """SELECT *, expires_at > clock_timestamp() AS live FROM leases
           WHERE tenant_id = %s AND contract_id = %s AND status = 'active'""",
        (tenant_id, contract_id))


def view(tx: Tx, c: dict, lease_token: str | None = None) -> dict:
    """Public representation. The lease token is only ever returned to the claimer."""
    out = dict(c)
    out["dependency_ids"] = dependency_ids(tx, c["tenant_id"], c["id"])
    lease = active_lease(tx, c["tenant_id"], c["id"])
    out["lease"] = None
    if lease:
        out["lease"] = {"holder_id": lease["holder_id"], "worker_id": lease["worker_id"],
                        "fencing_token": lease["fencing_token"], "expires_at": lease["expires_at"],
                        "expired": not lease["live"]}
        if lease_token and lease["token"] == lease_token:
            out["lease"]["lease_token"] = lease_token
    return out


def list_contracts(tx: Tx, tenant_id: str, goal_id: str | None, status: str | None,
                   cursor: str | None, limit: int) -> dict:
    rows = tx.all(
        """SELECT * FROM contracts WHERE tenant_id = %(t)s AND id > %(cursor)s
             AND (%(goal)s::text IS NULL OR goal_id = %(goal)s)
             AND (%(status)s::text IS NULL OR status = %(status)s)
           ORDER BY id LIMIT %(limit)s""",
        {"t": tenant_id, "goal": goal_id, "status": status, "cursor": cursor or "", "limit": limit + 1})
    return {"items": [view(tx, r) for r in rows[:limit]],
            "next_cursor": rows[limit - 1]["id"] if len(rows) > limit else None}


# ---------------------------------------------------------------- authorization helpers

def _goal(tx: Tx, tenant_id: str, goal_id: str) -> dict:
    return tx.one("SELECT * FROM goals WHERE tenant_id = %s AND id = %s", (tenant_id, goal_id))


def _has_authority(ctx: Ctx, c: dict) -> bool:
    """Requester of the contract, owner of its goal, or a human admin."""
    if ctx.actor_id == c["requester_id"] or policy.is_admin(ctx):
        return True
    return ctx.is_human and _goal(ctx.tx, ctx.tenant_id, c["goal_id"])["owner_principal_id"] == ctx.actor_id


def _require_authority(ctx: Ctx, c: dict, action: str) -> None:
    if not _has_authority(ctx, c):
        raise policy.deny(ctx, "only the contract's requester, the goal owner or an admin may do this",
                          action=action, resource_type="contract", resource_id=c["id"], goal_id=c["goal_id"])


def require_lease(ctx: Ctx, c: dict, lease_token: str) -> dict:
    """Fencing check (spec §3): the token must name the contract's one active, unexpired lease
    for the current fencing token, and the caller must be its holder."""
    lease = ctx.tx.one(
        """SELECT *, expires_at > clock_timestamp() AS live FROM leases
           WHERE tenant_id = %s AND contract_id = %s AND %s = token""",
        (ctx.tenant_id, c["id"], lease_token or ""))
    if lease is None:
        raise Conflict("lease token is not valid for this contract", code="LEASE_INVALID")
    if lease["status"] != "active" or lease["fencing_token"] != c["fencing_token"]:
        raise Conflict("this lease has been fenced; the attempt it belonged to is over",
                       code="LEASE_FENCED", details={"lease_status": lease["status"],
                                                     "lease_fencing_token": lease["fencing_token"],
                                                     "current_fencing_token": c["fencing_token"]})
    if lease["holder_id"] != ctx.actor_id:
        raise policy.deny(ctx, "this lease belongs to another principal", action="lease.use",
                          resource_type="contract", resource_id=c["id"], goal_id=c["goal_id"])
    if not lease["live"]:
        raise Conflict("this lease has expired", code="LEASE_EXPIRED")
    return lease


# ---------------------------------------------------------------- transitions and fencing

def _transition(ctx: Ctx, c: dict, to: str, *, reason: str | None = None, payload: dict | None = None,
                **fields) -> dict:
    states.check_transition(c["status"], to)
    fields.setdefault("blocked_reason", reason if to == states.BLOCKED else None)
    if to == states.READY:
        # A new attempt is open to whoever the contract is assigned to, not to the last holder.
        fields.setdefault("owner_id", c["assigned_owner_id"])
    new = bump(ctx.tx, "contracts", ctx.tenant_id, c["id"], status=to, status_reason=reason, **fields)
    emit(ctx, f"cas.contract.{to}.v1", "contract", c["id"], new["version"], c["goal_id"],
         {"from_status": c["status"], "to_status": to, "reason": reason, "attempt": new["attempt"],
          "fencing_token": new["fencing_token"], **(payload or {})})
    return new


def _fence(ctx: Ctx, c: dict, attempt_status: str, reason: str) -> None:
    """End the contract's current attempt so nothing it does later can be committed: the lease
    stops validating, the attempt is closed, and its proposed artifacts are rejected. A fence
    does not prove the worker stopped; it only guarantees CAS will refuse its writes."""
    handed_over = attempt_status in ("submitted", "accepted")
    ctx.tx.execute(
        "UPDATE leases SET status = %s, updated_at = now() WHERE tenant_id = %s AND contract_id = %s AND status = 'active'",
        ("released" if handed_over else "fenced", ctx.tenant_id, c["id"]))
    if handed_over:
        ctx.tx.execute(
            """UPDATE contract_attempts SET status = %s, outcome_reason = %s,
                      ended_at = CASE WHEN %s = 'accepted' THEN now() ELSE ended_at END
               WHERE tenant_id = %s AND contract_id = %s AND fencing_token = %s""",
            (attempt_status, reason, attempt_status, ctx.tenant_id, c["id"], c["fencing_token"]))
        return
    ctx.tx.execute(
        """UPDATE contract_attempts SET status = %s, outcome_reason = %s, ended_at = now()
           WHERE tenant_id = %s AND contract_id = %s AND fencing_token = %s
             AND status IN ('leased','running','submitted','validating')""",
        (attempt_status, reason, ctx.tenant_id, c["id"], c["fencing_token"]))
    ctx.tx.execute(
        """UPDATE adapter_runs SET status = 'cancelled', updated_at = now()
           WHERE tenant_id = %s AND contract_id = %s AND fencing_token = %s AND status IN ('starting','running')""",
        (ctx.tenant_id, c["id"], c["fencing_token"]))
    artifacts.reject_attempt_versions(ctx, c, reason)
    _close_open_effects(ctx, c)


def _close_open_effects(ctx: Ctx, c: dict) -> None:
    """An effect authorized but never reported has an unknown outcome. If the step is idempotent it
    can simply run again; otherwise a human has to establish what happened (spec §9, T16)."""
    rows = ctx.tx.all(
        """UPDATE external_effects
              SET status = CASE WHEN idempotent THEN 'failed' ELSE 'manual_recovery_required' END,
                  detail = detail || '{"outcome":"unknown"}'::jsonb, updated_at = now(), version = version + 1
            WHERE tenant_id = %s AND contract_id = %s AND fencing_token = %s AND status = 'authorized'
            RETURNING *""",
        (ctx.tenant_id, c["id"], c["fencing_token"]))
    for e in rows:
        emit(ctx, f"cas.effect.{e['status']}.v1", "effect", e["id"], e["version"], c["goal_id"],
             {"contract_id": c["id"], "step_key": e["step_key"], "scope": e["scope"], "outcome": "unknown"})


# ---------------------------------------------------------------- readiness and pinning

def resolve_pins(tx: Tx, tenant_id: str, c: dict) -> tuple[list[dict], list[dict]]:
    """Resolve every input_ref to one accepted artifact version (spec §2). Returns (pins, unresolved)."""
    pins, missing = [], []
    for ref in c["input_refs"]:
        if ref["mode"] == "exact":
            row = tx.one(
                """SELECT version_id, sha256, status FROM artifact_versions
                   WHERE tenant_id = %s AND artifact_id = %s AND version_id = %s""",
                (tenant_id, ref["artifact_id"], ref["required_version"]))
        else:
            row = tx.one(
                """SELECT v.version_id, v.sha256, v.status FROM artifacts a JOIN artifact_versions v
                     ON v.tenant_id = a.tenant_id AND v.artifact_id = a.id AND v.version_id = a.current_version_id
                   WHERE a.tenant_id = %s AND a.id = %s""",
                (tenant_id, ref["artifact_id"]))
        if row is None or row["status"] != "accepted":
            missing.append({**ref, "found_status": row["status"] if row else None})
        else:
            pins.append({"artifact_id": ref["artifact_id"], "version_id": row["version_id"],
                         "sha256": row["sha256"], "mode": ref["mode"]})
    return pins, missing


def stale_pins(tx: Tx, tenant_id: str, c: dict) -> list[dict]:
    """Pinned versions that are no longer the accepted version of their artifact."""
    out = []
    for pin in c["pinned_inputs"] or []:
        row = tx.one(
            """SELECT v.status, a.current_version_id FROM artifact_versions v JOIN artifacts a
                 ON a.tenant_id = v.tenant_id AND a.id = v.artifact_id
               WHERE v.tenant_id = %s AND v.artifact_id = %s AND v.version_id = %s""",
            (tenant_id, pin["artifact_id"], pin["version_id"]))
        if row is None or row["status"] != "accepted":
            out.append({**pin, "current_version_id": row["current_version_id"] if row else None})
    return out


def readiness(tx: Tx, tenant_id: str, c: dict) -> tuple[bool, str, dict]:
    """Can this contract become READY right now? Returns (ok, reason, details)."""
    goal = _goal(tx, tenant_id, c["goal_id"])
    if goal["status"] != "active":
        return False, "goal_not_active", {"goal_status": goal["status"]}
    unmet = tx.all(
        """SELECT u.id, u.status FROM contract_dependencies d JOIN contracts u
             ON u.tenant_id = d.tenant_id AND u.id = d.upstream_id
           WHERE d.tenant_id = %s AND d.downstream_id = %s AND u.status <> 'accepted'""",
        (tenant_id, c["id"]))
    if unmet:
        return False, "dependencies_unmet", {"unmet": [{"id": u["id"], "status": u["status"]} for u in unmet]}
    hold = tx.one(
        """SELECT id FROM decisions WHERE tenant_id = %s AND state IN ('open','awaiting_authority')
             AND held_contract_ids @> %s::jsonb LIMIT 1""",
        (tenant_id, [c["id"]]))
    if hold:
        return False, "decision_hold", {"decision_id": hold["id"]}
    pins, missing = resolve_pins(tx, tenant_id, c)
    if missing:
        return False, "inputs_unresolved", {"unresolved": missing}
    wanted = set(c["permission_scope"] or [])
    grantable = policy.scopes(tx, tenant_id, c["requester_id"], c["goal_id"]) & policy.resource_scopes(
        tx, tenant_id, c["goal_id"])
    if not wanted <= grantable:
        return False, "policy_denied", {"ungrantable_scopes": sorted(wanted - grantable)}
    return True, "ok", {"pins": pins}


def reconcile_one(ctx: Ctx, contract_id: str) -> dict:
    """Bring one contract's status in line with its dependencies, inputs and policy (spec §10)."""
    c = get(ctx.tx, ctx.tenant_id, contract_id, lock=True)
    if c["status"] in states.TERMINAL:
        return c

    if c["status"] in (states.READY, states.LEASED, states.RUNNING):
        stale = stale_pins(ctx.tx, ctx.tenant_id, c)
        if stale:
            _fence(ctx, c, "fenced", "stale_input")
            c = _transition(ctx, c, states.BLOCKED, reason="stale_input", payload={"stale_pins": stale})

    if c["status"] in (states.SUBMITTED, states.VALIDATING) and (
            c["stale_inputs"] or stale_pins(ctx.tx, ctx.tenant_id, c)):
        if c["status"] == states.SUBMITTED:
            c = _transition(ctx, c, states.VALIDATING)
        _fence(ctx, c, "rejected", "stale_input")
        c = _transition(ctx, c, states.REJECTED, reason="stale_input", stale_inputs=False)

    if c["status"] in (states.PROPOSED, states.BLOCKED):
        auto = c["status"] == states.PROPOSED or c["blocked_reason"] in AUTO_UNBLOCK or (
            c["blocked_reason"] == "stale_input" and c["escalation_policy"]["on_stale"] == "revalidate")
        if auto:
            ok, reason, details = readiness(ctx.tx, ctx.tenant_id, c)
            if ok:
                c = _transition(ctx, c, states.READY, pinned_inputs=details["pins"], stale_inputs=False,
                                payload={"pinned_inputs": details["pins"]})
            elif c["blocked_reason"] == "decision_hold" and reason != "decision_hold":
                # The decision is closed but something else still stands in the way; say what.
                c = bump(ctx.tx, "contracts", ctx.tenant_id, c["id"], blocked_reason="dependency_unmet",
                         status_reason="dependency_unmet")
                emit(ctx, "cas.contract.hold_released.v1", "contract", c["id"], c["version"], c["goal_id"],
                     {"released": "decision_hold", "still_waiting_on": reason, **{k: v for k, v in details.items() if k != "pins"}})
            elif reason == "policy_denied" and c["status"] == states.PROPOSED:
                c = _transition(ctx, c, states.BLOCKED, reason="policy_denied", payload=details)
                emit(ctx, "cas.policy.denied.v1", "contract", c["id"], c["version"], c["goal_id"],
                     {"action": "contract.start", "principal_id": c["requester_id"], **details})

    if c["status"] in (states.REJECTED, states.FAILED):
        c = _maybe_auto_retry(ctx, c)
    return c


def reconcile_goal(ctx: Ctx, goal_id: str) -> None:
    for r in ctx.tx.all(
            """SELECT id FROM contracts WHERE tenant_id = %s AND goal_id = %s AND status IN ('proposed','blocked')
               ORDER BY priority DESC, created_at""", (ctx.tenant_id, goal_id)):
        reconcile_one(ctx, r["id"])


# ---------------------------------------------------------------- invalidation (spec §4)

def propagate_invalidation(ctx: Ctx, artifact_id: str, old_version: str, new_version: str | None) -> list[str]:
    """An accepted artifact version was superseded. Hold every contract whose live attempt is
    pinned to it, and flag accepted work built on it for reassessment."""
    affected = ctx.tx.all(
        """SELECT id FROM contracts WHERE tenant_id = %s AND pinned_inputs @> %s::jsonb ORDER BY id FOR UPDATE""",
        (ctx.tenant_id, [{"artifact_id": artifact_id, "version_id": old_version}]))
    out = []
    for r in affected:
        c = get(ctx.tx, ctx.tenant_id, r["id"])
        base = {"input_artifact_id": artifact_id, "old_version": old_version, "new_version": new_version}
        if c["status"] in (states.READY, states.LEASED, states.RUNNING):
            c = bump(ctx.tx, "contracts", ctx.tenant_id, c["id"], stale_inputs=True)
            evt = emit(ctx, "cas.input.invalidated.v1", "contract", c["id"], c["version"], c["goal_id"],
                       {**base, "action": "hold_and_revalidate"})
            cause = ctx.caused_by(evt)
            _fence(cause, c, "fenced", "stale_input")
            _transition(cause, c, states.BLOCKED, reason="stale_input", payload=base)
            reconcile_one(cause, c["id"])
        elif c["status"] in (states.SUBMITTED, states.VALIDATING):
            c = bump(ctx.tx, "contracts", ctx.tenant_id, c["id"], stale_inputs=True)
            emit(ctx, "cas.input.invalidated.v1", "contract", c["id"], c["version"], c["goal_id"],
                 {**base, "action": "reject_validation"})
        elif c["status"] == states.ACCEPTED:
            c = bump(ctx.tx, "contracts", ctx.tenant_id, c["id"], requires_reassessment=True)
            emit(ctx, "cas.contract.reassessment_required.v1", "contract", c["id"], c["version"], c["goal_id"],
                 {**base, "action": "create_successor"})
        else:
            continue
        out.append(c["id"])
    return out


# ---------------------------------------------------------------- create / revise / dependencies

def _lock_graph(ctx: Ctx, goal_id: str) -> None:
    """Serialise dependency-graph changes per goal so the cycle check is transactionally consistent."""
    ctx.tx.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (f"{ctx.tenant_id}/{goal_id}/graph",))


def _check_refs(ctx: Ctx, doc: dict, goal_id: str) -> None:
    require_principal(ctx.tx, ctx.tenant_id, doc["requester_id"], "requester_id")
    if doc.get("owner_id"):
        require_principal(ctx.tx, ctx.tenant_id, doc["owner_id"], "owner_id")
    require_principal(ctx.tx, ctx.tenant_id, doc["escalation_policy"]["decision_owner_id"],
                      "escalation_policy.decision_owner_id")
    if doc.get("parent_contract_id"):
        parent = ctx.tx.one("SELECT goal_id FROM contracts WHERE tenant_id = %s AND id = %s",
                            (ctx.tenant_id, doc["parent_contract_id"]))
        if parent is None or parent["goal_id"] != goal_id:
            raise Unprocessable("parent_contract_id must be a contract of the same goal", code="UNKNOWN_CONTRACT")


def _insert(ctx: Ctx, doc: dict, supersedes: str | None = None) -> dict:
    return ctx.tx.one(
        """INSERT INTO contracts (tenant_id, id, goal_id, parent_contract_id, supersedes_contract_id, title,
               objective, requester_id, owner_id, assigned_owner_id, input_refs, deliverable_spec, acceptance_checks,
               permission_scope, priority, deadline, max_attempts, escalation_policy)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *""",
        (ctx.tenant_id, new_id("contract"), doc["goal_id"], doc.get("parent_contract_id"), supersedes,
         doc["title"], doc["objective"], doc["requester_id"], doc.get("owner_id") or None, doc.get("owner_id") or None,
         doc["input_refs"],
         doc["deliverable_spec"], doc["acceptance_checks"], sorted(doc["permission_scope"]),
         doc.get("priority", 50), doc.get("deadline"), doc["max_attempts"], doc["escalation_policy"]))


def _insert_dependencies(ctx: Ctx, c: dict, upstream_ids: list[str]) -> None:
    for up in upstream_ids:
        row = ctx.tx.one("SELECT goal_id FROM contracts WHERE tenant_id = %s AND id = %s", (ctx.tenant_id, up))
        if row is None or row["goal_id"] != c["goal_id"]:
            raise Unprocessable(f"dependency '{up}' is not a contract of this goal", code="UNKNOWN_CONTRACT",
                                details={"dependency_id": up})
        ctx.tx.execute(
            "INSERT INTO contract_dependencies (tenant_id, upstream_id, downstream_id) VALUES (%s,%s,%s)",
            (ctx.tenant_id, up, c["id"]))


def create(ctx: Ctx, body: dict) -> dict:
    schemas.validate("contract", body)
    goal = _goal(ctx.tx, ctx.tenant_id, body["goal_id"])
    if goal is None:
        raise Unprocessable(f"goal '{body['goal_id']}' not found", code="UNKNOWN_GOAL")
    if goal["status"] in ("completed", "cancelled"):
        raise Conflict(f"goal is {goal['status']}; it accepts no new work", code="GOAL_CLOSED")
    owns_goal = ctx.is_human and goal["owner_principal_id"] == ctx.actor_id
    if not (owns_goal or policy.is_admin(ctx)
            or policy.has_role(ctx.tx, ctx.tenant_id, ctx.actor_id, "task_requester", goal["id"])):
        raise policy.deny(ctx, "proposing a contract requires the 'task_requester' role on the goal",
                          action="contract.create", resource_type="goal", resource_id=goal["id"], goal_id=goal["id"])
    if body["requester_id"] != ctx.actor_id and not (owns_goal or policy.is_admin(ctx)):
        raise policy.deny(ctx, "requester_id must be the calling principal", action="contract.create",
                          resource_type="goal", resource_id=goal["id"], goal_id=goal["id"])
    _check_refs(ctx, body, goal["id"])
    _lock_graph(ctx, goal["id"])
    c = _insert(ctx, body)
    _insert_dependencies(ctx, c, body["dependency_ids"])
    evt = emit(ctx, "cas.contract.proposed.v1", "contract", c["id"], c["version"], c["goal_id"],
               {"to_status": "proposed", "title": c["title"], "requester_id": c["requester_id"],
                "owner_id": c["owner_id"], "dependency_ids": body["dependency_ids"]})
    return reconcile_one(ctx.caused_by(evt), c["id"])


def _document(tx: Tx, c: dict) -> dict:
    """The contract as a schema-shaped document, for validating a revision."""
    doc = {k: c[k] for k in SCHEMA_FIELDS if c.get(k) is not None}
    doc["dependency_ids"] = dependency_ids(tx, c["tenant_id"], c["id"])
    if "deadline" in doc and not isinstance(doc["deadline"], str):
        doc["deadline"] = doc["deadline"].isoformat()
    return doc


def _plain(d: dict) -> dict:
    return {k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in d.items()}


def revise(ctx: Ctx, contract_id: str, body: dict, expected_version: int | None) -> dict:
    """Revise a contract. Before execution the fields change in place. An accepted contract is
    terminal, so revising it creates a linked successor instead (spec §3)."""
    c = get(ctx.tx, ctx.tenant_id, contract_id, lock=True)
    _require_authority(ctx, c, "contract.revise")
    check_version(c, expected_version, "contract")
    changes = body.get("changes") or {}
    unknown = sorted(set(changes) - set(REVISABLE_FIELDS))
    if unknown:
        raise Unprocessable(f"these fields cannot be revised: {', '.join(unknown)}. Use the dependencies "
                            "endpoint to change dependencies.", code="FIELD_NOT_REVISABLE")
    doc = {**_document(ctx.tx, c), **{k: v for k, v in changes.items() if v is not None}}
    for k, v in changes.items():
        if v is None:
            doc.pop(k, None)
    schemas.validate("contract", doc)
    _check_refs(ctx, doc, c["goal_id"])

    if c["status"] == states.ACCEPTED:
        _lock_graph(ctx, c["goal_id"])
        successor = _insert(ctx, doc, supersedes=c["id"])
        _insert_dependencies(ctx, successor, doc["dependency_ids"])
        evt = emit(ctx, "cas.contract.proposed.v1", "contract", successor["id"], successor["version"],
                   c["goal_id"], {"to_status": "proposed", "title": successor["title"],
                                  "supersedes_contract_id": c["id"], "rationale": body.get("rationale", ""),
                                  "dependency_ids": doc["dependency_ids"]})
        return reconcile_one(ctx.caused_by(evt), successor["id"])

    if c["status"] not in (states.PROPOSED, states.BLOCKED, states.REJECTED, states.FAILED):
        raise Conflict(f"a contract in status '{c['status']}' cannot be revised; block or cancel it first",
                       code="CONTRACT_NOT_REVISABLE", details={"status": c["status"]})
    if not changes:
        raise Unprocessable("changes is empty", code="NO_CHANGES")
    before = _plain({k: c[k] for k in changes})
    fields = {k: (sorted(doc[k]) if k == "permission_scope" else doc.get(k)) for k in changes}
    if "permission_scope" in changes:
        fields["policy_version"] = c["policy_version"] + 1
    if "owner_id" in changes:
        fields["assigned_owner_id"] = fields["owner_id"]
    new = bump(ctx.tx, "contracts", ctx.tenant_id, c["id"], **fields)
    after = _plain({k: new[k] for k in changes})
    ctx.tx.execute(
        """INSERT INTO contract_revisions (tenant_id, id, contract_id, contract_version, actor_id, before, after)
           VALUES (%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb)""",
        (ctx.tenant_id, new_id("crev"), c["id"], new["version"], ctx.actor_id, json.dumps(before), json.dumps(after)))
    evt = emit(ctx, "cas.contract.revised.v1", "contract", c["id"], new["version"], c["goal_id"],
               {"before": before, "after": after, "rationale": body.get("rationale", "")})
    return reconcile_one(ctx.caused_by(evt), c["id"])


def add_dependency(ctx: Ctx, downstream_id: str, upstream_id: str, expected_version: int | None) -> dict:
    """Add 'downstream depends on upstream'. Rejects cycles without touching the graph (spec §4, T08)."""
    head = get(ctx.tx, ctx.tenant_id, downstream_id)
    _lock_graph(ctx, head["goal_id"])
    c = get(ctx.tx, ctx.tenant_id, downstream_id, lock=True)
    if not (_has_authority(ctx, c) or ctx.actor_id == c["owner_id"]):
        raise policy.deny(ctx, "only the contract's requester, owner, the goal owner or an admin may change its dependencies",
                          action="contract.add_dependency", resource_type="contract", resource_id=c["id"],
                          goal_id=c["goal_id"])
    check_version(c, expected_version, "contract")
    if upstream_id == downstream_id:
        raise Unprocessable("a contract cannot depend on itself", code="DEPENDENCY_CYCLE")
    up = ctx.tx.one("SELECT id, goal_id, status FROM contracts WHERE tenant_id = %s AND id = %s",
                    (ctx.tenant_id, upstream_id))
    if up is None or up["goal_id"] != c["goal_id"]:
        raise Unprocessable(f"'{upstream_id}' is not a contract of this goal", code="UNKNOWN_CONTRACT")
    if c["status"] in states.TERMINAL or c["status"] in (states.SUBMITTED, states.VALIDATING):
        raise Conflict(f"dependencies cannot change while the contract is '{c['status']}'",
                       code="CONTRACT_NOT_REVISABLE", details={"status": c["status"]})
    if ctx.tx.one("SELECT 1 FROM contract_dependencies WHERE tenant_id = %s AND upstream_id = %s AND downstream_id = %s",
                  (ctx.tenant_id, upstream_id, downstream_id)):
        raise Conflict("this dependency already exists", code="DEPENDENCY_EXISTS")
    cycle = ctx.tx.one(
        """WITH RECURSIVE ancestors AS (
             SELECT upstream_id FROM contract_dependencies WHERE tenant_id = %(t)s AND downstream_id = %(up)s
             UNION
             SELECT d.upstream_id FROM contract_dependencies d JOIN ancestors a ON d.downstream_id = a.upstream_id
              WHERE d.tenant_id = %(t)s)
           SELECT 1 FROM ancestors WHERE upstream_id = %(down)s LIMIT 1""",
        {"t": ctx.tenant_id, "up": upstream_id, "down": downstream_id})
    if cycle:
        raise Unprocessable(f"'{upstream_id}' already depends on '{downstream_id}'; this dependency would create a cycle",
                            code="DEPENDENCY_CYCLE")
    ctx.tx.execute("INSERT INTO contract_dependencies (tenant_id, upstream_id, downstream_id) VALUES (%s,%s,%s)",
                   (ctx.tenant_id, upstream_id, downstream_id))
    new = bump(ctx.tx, "contracts", ctx.tenant_id, c["id"])
    evt = emit(ctx, "cas.contract.dependency_added.v1", "contract", c["id"], new["version"], c["goal_id"],
               {"upstream_id": upstream_id, "upstream_status": up["status"]})
    if up["status"] != states.ACCEPTED and new["status"] in (states.READY, states.LEASED, states.RUNNING):
        cause = ctx.caused_by(evt)
        _fence(cause, new, "fenced", "dependency_unmet")
        new = _transition(cause, new, states.BLOCKED, reason="dependency_unmet", payload={"upstream_id": upstream_id})
    return new


# ---------------------------------------------------------------- leases

def _grant_lease(ctx: Ctx, c: dict, holder_id: str, worker_id: str, lease_seconds: int | None) -> tuple[dict, str]:
    seconds = max(1, min(int(lease_seconds or settings.lease_default_seconds), settings.lease_max_seconds))
    issued = "lease_" + secrets.token_urlsafe(24)
    nxt = c["fencing_token"] + 1
    ctx.tx.execute(
        """INSERT INTO leases (tenant_id, id, contract_id, fencing_token, holder_id, worker_id, token, status, expires_at)
           VALUES (%s,%s,%s,%s,%s,%s,%s,'active', clock_timestamp() + make_interval(secs => %s))""",
        (ctx.tenant_id, new_id("lease"), c["id"], nxt, holder_id, worker_id, issued, seconds))
    ctx.tx.execute(
        """INSERT INTO contract_attempts (tenant_id, id, contract_id, attempt_no, fencing_token, owner_id, status, pinned_inputs)
           VALUES (%s,%s,%s,%s,%s,%s,'leased',%s)""",
        (ctx.tenant_id, new_id("attempt"), c["id"], c["attempt"], nxt, holder_id, c["pinned_inputs"]))
    new = _transition(ctx, c, states.LEASED, owner_id=holder_id, fencing_token=nxt,
                      payload={"owner_id": holder_id, "worker_id": worker_id, "lease_seconds": seconds})
    return new, issued


def can_hold(tx: Tx, tenant_id: str, c: dict, agent_id: str) -> tuple[bool, str, dict]:
    """Authority to take the contract (spec §6). A capability claim plays no part here."""
    if not policy.has_role(tx, tenant_id, agent_id, "task_owner", c["goal_id"]):
        return False, "claiming requires the 'task_owner' role on the goal", {}
    if c["assigned_owner_id"] and c["assigned_owner_id"] != agent_id:
        return False, "this contract is assigned to another owner", {"owner_id": c["assigned_owner_id"]}
    missing = set(c["permission_scope"] or []) - policy.scopes(tx, tenant_id, agent_id, c["goal_id"])
    if missing:
        return False, "the agent's grants do not cover the contract's permission scope", {"missing_scopes": sorted(missing)}
    return True, "", {}


def at_capacity(tx: Tx, tenant_id: str, agent_id: str) -> tuple[bool, int]:
    limit = tx.scalar(
        """SELECT max((limits->>'max_concurrent')::int) FROM agent_capabilities
           WHERE tenant_id = %s AND agent_id = %s AND limits ? 'max_concurrent'""",
        (tenant_id, agent_id)) or DEFAULT_MAX_CONCURRENT
    held = tx.scalar(
        "SELECT count(*) FROM leases WHERE tenant_id = %s AND holder_id = %s AND status = 'active' AND expires_at > clock_timestamp()",
        (tenant_id, agent_id))
    return held >= limit, limit


def claim(ctx: Ctx, contract_id: str, worker_id: str, lease_seconds: int | None,
          expected_version: int | None = None) -> tuple[dict, str]:
    """Atomically claim a ready contract (spec §6, T04). The row lock makes concurrent claimers
    queue; the loser then sees a contract that is no longer ready."""
    c = get(ctx.tx, ctx.tenant_id, contract_id, lock=True)
    ok, why, details = can_hold(ctx.tx, ctx.tenant_id, c, ctx.actor_id)
    if not ok:
        raise policy.deny(ctx, why, action="contract.claim", resource_type="contract", resource_id=c["id"],
                          goal_id=c["goal_id"], details=details)
    check_version(c, expected_version, "contract")
    states.check_transition(c["status"], states.LEASED)
    if _goal(ctx.tx, ctx.tenant_id, c["goal_id"])["status"] != "active":
        raise Conflict("the goal is not active", code="GOAL_NOT_ACTIVE")
    if stale_pins(ctx.tx, ctx.tenant_id, c):
        raise Conflict("the contract's pinned inputs are stale; it is awaiting reconciliation", code="STALE_INPUT")
    full, limit = at_capacity(ctx.tx, ctx.tenant_id, ctx.actor_id)
    if full:
        raise QuotaExceeded(f"agent already holds {limit} active leases", code="CONCURRENCY_LIMIT")
    return _grant_lease(ctx, c, ctx.actor_id, worker_id, lease_seconds)


def heartbeat(ctx: Ctx, contract_id: str, lease_token: str, lease_seconds: int | None) -> dict:
    c = get(ctx.tx, ctx.tenant_id, contract_id, lock=True)
    lease = require_lease(ctx, c, lease_token)
    seconds = max(1, min(int(lease_seconds or settings.lease_default_seconds), settings.lease_max_seconds))
    ctx.tx.execute(
        "UPDATE leases SET expires_at = clock_timestamp() + make_interval(secs => %s), updated_at = now() WHERE tenant_id = %s AND id = %s",
        (seconds, ctx.tenant_id, lease["id"]))
    return c


def start(ctx: Ctx, contract_id: str, lease_token: str) -> dict:
    c = get(ctx.tx, ctx.tenant_id, contract_id, lock=True)
    require_lease(ctx, c, lease_token)
    new = _transition(ctx, c, states.RUNNING)
    ctx.tx.execute(
        "UPDATE contract_attempts SET status = 'running' WHERE tenant_id = %s AND contract_id = %s AND fencing_token = %s",
        (ctx.tenant_id, c["id"], c["fencing_token"]))
    return new


def expire_lease(ctx: Ctx, contract_id: str) -> dict:
    """Called by the reconciler when a held lease has run out (T05). The attempt is fenced and the
    contract fails; expiry is not evidence that the worker stopped."""
    c = get(ctx.tx, ctx.tenant_id, contract_id, lock=True)
    lease = active_lease(ctx.tx, ctx.tenant_id, c["id"])
    if c["status"] not in states.LEASE_HELD or lease is None or lease["live"]:
        return c
    ctx.tx.execute("UPDATE leases SET status = 'expired', updated_at = now() WHERE tenant_id = %s AND id = %s",
                   (ctx.tenant_id, lease["id"]))
    _fence(ctx, c, "failed", "lease_expired")
    new = _transition(ctx, c, states.FAILED, reason="lease_expired", not_before=_backoff(ctx, c),
                      payload={"holder_id": lease["holder_id"], "worker_id": lease["worker_id"]})
    return _maybe_auto_retry(ctx, new)


# ---------------------------------------------------------------- holds, failure, retry, cancel

def block(ctx: Ctx, contract_id: str, note: str, lease_token: str | None, expected_version: int | None) -> dict:
    c = get(ctx.tx, ctx.tenant_id, contract_id, lock=True)
    if lease_token:
        require_lease(ctx, c, lease_token)
    else:
        _require_authority(ctx, c, "contract.block")
    check_version(c, expected_version, "contract")
    states.check_transition(c["status"], states.BLOCKED)
    _fence(ctx, c, "fenced", "explicit_hold")
    return _transition(ctx, c, states.BLOCKED, reason="explicit_hold", payload={"note": note})


def revalidate(ctx: Ctx, contract_id: str, expected_version: int | None) -> dict:
    """Explicit revalidation of a blocked contract (spec §3, blocked -> ready)."""
    c = get(ctx.tx, ctx.tenant_id, contract_id, lock=True)
    _require_authority(ctx, c, "contract.revalidate")
    check_version(c, expected_version, "contract")
    if c["status"] != states.BLOCKED:
        raise states.InvalidTransition("only a blocked contract can be revalidated",
                                       details={"from": c["status"], "to": states.READY})
    ok, reason, details = readiness(ctx.tx, ctx.tenant_id, c)
    if not ok:
        raise Unprocessable(f"the contract cannot become ready yet: {reason}", code="NOT_READY",
                            details={"reason": reason, **details})
    return _transition(ctx, c, states.READY, pinned_inputs=details["pins"], stale_inputs=False,
                       payload={"pinned_inputs": details["pins"], "revalidated_by": ctx.actor_id})


def _backoff(ctx: Ctx, c: dict):
    """Exponential backoff with jitter for the next automatic retry (spec §13)."""
    delay = min(settings.retry_backoff_base_seconds * (2 ** (c["attempt"] - 1)), settings.retry_backoff_max_seconds)
    delay *= random.uniform(0.5, 1.0)
    return ctx.tx.scalar("SELECT clock_timestamp() + make_interval(secs => %s)", (delay,))


def fail(ctx: Ctx, contract_id: str, lease_token: str, reason: str, retryable: bool) -> dict:
    c = get(ctx.tx, ctx.tenant_id, contract_id, lock=True)
    require_lease(ctx, c, lease_token)
    states.check_transition(c["status"], states.FAILED)
    _fence(ctx, c, "failed", reason or "execution_error")
    new = _transition(ctx, c, states.FAILED,
                      reason="execution_error_retryable" if retryable else "execution_error",
                      not_before=_backoff(ctx, c), payload={"detail": reason})
    return _maybe_auto_retry(ctx, new)


def _needs_manual_recovery(tx: Tx, tenant_id: str, contract_id: str) -> list[dict]:
    return tx.all(
        """SELECT id, step_key, scope FROM external_effects
           WHERE tenant_id = %s AND contract_id = %s AND status = 'manual_recovery_required'""",
        (tenant_id, contract_id))


def _retry(ctx: Ctx, c: dict, pins: list[dict], automatic: bool) -> dict:
    effects = ctx.tx.all("SELECT step_key, status, detail FROM external_effects WHERE tenant_id = %s AND contract_id = %s",
                         (ctx.tenant_id, c["id"]))
    done = sorted({e["step_key"] for e in effects
                   if e["status"] == "succeeded" or (e["status"] == "recovered" and e["detail"].get("outcome") == "applied")})
    return _transition(ctx, c, states.READY, attempt=c["attempt"] + 1, pinned_inputs=pins, stale_inputs=False,
                       not_before=None,
                       payload={"automatic": automatic, "previous_attempt": c["attempt"], "pinned_inputs": pins,
                                "effects_carried_over": done,
                                "effects_to_rerun": sorted({e["step_key"] for e in effects} - set(done))})


def _maybe_auto_retry(ctx: Ctx, c: dict) -> dict:
    """Bounded automatic retry (spec §13): only for transient failures, within the retry budget,
    after the backoff, and never while a side effect needs manual recovery."""
    stale_reject = (c["status"] == states.REJECTED and c["status_reason"] == "stale_input"
                    and c["escalation_policy"]["on_stale"] == "revalidate")
    transient = c["status"] == states.FAILED and c["status_reason"] in AUTO_RETRY_REASONS
    if not (stale_reject or transient) or c["attempt"] >= c["max_attempts"]:
        return c
    if _needs_manual_recovery(ctx.tx, ctx.tenant_id, c["id"]):
        return c
    if c["not_before"] is not None and not ctx.tx.scalar("SELECT %s <= clock_timestamp()", (c["not_before"],)):
        return c
    ok, _, details = readiness(ctx.tx, ctx.tenant_id, c)
    return _retry(ctx, c, details["pins"], automatic=True) if ok else c


def retry(ctx: Ctx, contract_id: str, expected_version: int | None) -> dict:
    """Explicit retry of a rejected or failed contract: a new attempt with refreshed pins (T11)."""
    c = get(ctx.tx, ctx.tenant_id, contract_id, lock=True)
    _require_authority(ctx, c, "contract.retry")
    check_version(c, expected_version, "contract")
    states.check_transition(c["status"], states.READY)
    if c["status"] not in (states.REJECTED, states.FAILED):
        raise states.InvalidTransition("only a rejected or failed contract can be retried",
                                       details={"from": c["status"], "to": states.READY})
    if c["attempt"] >= c["max_attempts"]:
        raise Unprocessable(f"retry budget exhausted ({c['attempt']} of {c['max_attempts']} attempts used); "
                            "revise max_attempts to allow more", code="RETRY_BUDGET_EXHAUSTED")
    pending = _needs_manual_recovery(ctx.tx, ctx.tenant_id, c["id"])
    if pending:
        raise Conflict("a side effect with an unknown or failed outcome is not safe to repeat; "
                       "record its recovery before retrying", code="MANUAL_RECOVERY_REQUIRED",
                       details={"effects": pending})
    ok, reason, details = readiness(ctx.tx, ctx.tenant_id, c)
    if not ok:
        raise Unprocessable(f"the contract cannot become ready yet: {reason}", code="NOT_READY",
                            details={"reason": reason, **details})
    return _retry(ctx, c, details["pins"], automatic=False)


def cancel(ctx: Ctx, contract_id: str, reason: str, expected_version: int | None) -> dict:
    """Authorized cancellation. The fence makes every later write from the old attempt fail (T18)."""
    c = get(ctx.tx, ctx.tenant_id, contract_id, lock=True)
    _require_authority(ctx, c, "contract.cancel")
    check_version(c, expected_version, "contract")
    return cancel_internal(ctx, c, reason)


def cancel_internal(ctx: Ctx, c: dict, reason: str) -> dict:
    states.check_transition(c["status"], states.CANCELLED)
    _fence(ctx, c, "cancelled", "cancelled")
    return _transition(ctx, c, states.CANCELLED, reason="cancelled", payload={"note": reason})


def hold_for(ctx: Ctx, contract_id: str, reason: str, payload: dict) -> dict | None:
    """Fence and block a contract on behalf of a decision or goal revision, if it can be blocked."""
    c = get(ctx.tx, ctx.tenant_id, contract_id, lock=True)
    if not states.can_transition(c["status"], states.BLOCKED):
        return None
    _fence(ctx, c, "fenced", reason)
    return _transition(ctx, c, states.BLOCKED, reason=reason, payload=payload)


# ---------------------------------------------------------------- submission and validation

def register_artifact(ctx: Ctx, contract_id: str, lease_token: str, body: dict) -> tuple[dict, dict]:
    c = get(ctx.tx, ctx.tenant_id, contract_id, lock=True)
    require_lease(ctx, c, lease_token)
    if c["status"] != states.RUNNING:
        raise states.InvalidTransition(
            f"artifacts can only be registered while the contract is running, not '{c['status']}'",
            details={"status": c["status"]})
    return c, artifacts.register_for_contract(ctx, c, body)


def submit(ctx: Ctx, contract_id: str, lease_token: str) -> dict:
    """running -> submitted. The owner's claim of completion; acceptance is a separate decision."""
    c = get(ctx.tx, ctx.tenant_id, contract_id, lock=True)
    require_lease(ctx, c, lease_token)
    states.check_transition(c["status"], states.SUBMITTED)
    stale = stale_pins(ctx.tx, ctx.tenant_id, c)
    if stale:
        raise Conflict("pinned inputs were superseded; this attempt can no longer be submitted",
                       code="STALE_INPUT", details={"stale_pins": stale})
    produced = ctx.tx.all(
        """SELECT a.artifact_type, v.schema_ref FROM artifact_versions v JOIN artifacts a
             ON a.tenant_id = v.tenant_id AND a.id = v.artifact_id
           WHERE v.tenant_id = %s AND v.contract_id = %s AND v.fencing_token = %s AND v.status = 'proposed'""",
        (ctx.tenant_id, c["id"], c["fencing_token"]))
    missing = []
    for spec in c["deliverable_spec"]:
        ok = any(p["artifact_type"] == spec["type"]
                 and (not spec.get("schema_ref") or p["schema_ref"] == spec["schema_ref"]) for p in produced)
        if not ok:
            missing.append(spec)
    if missing:
        raise Unprocessable("the attempt has not registered every required deliverable",
                            code="DELIVERABLES_MISSING", details={"missing": missing})
    _fence(ctx, c, "submitted", "submitted")
    return _transition(ctx, c, states.SUBMITTED, payload={"submitted_by": ctx.actor_id})


def begin_validation(ctx: Ctx, c: dict) -> dict:
    new = _transition(ctx, c, states.VALIDATING, payload={"validator_id": ctx.actor_id})
    ctx.tx.execute(
        "UPDATE contract_attempts SET status = 'validating' WHERE tenant_id = %s AND contract_id = %s AND fencing_token = %s",
        (ctx.tenant_id, c["id"], c["fencing_token"]))
    return new


def _require_validator(ctx: Ctx, c: dict, action: str) -> None:
    """Validation is separate from the producer's self-report (spec §15)."""
    policy.require_role(ctx, "validator", c["goal_id"], action=action, resource_type="contract", resource_id=c["id"])
    if ctx.actor_id == c["owner_id"]:
        raise policy.deny(ctx, "a contract's owner cannot validate its own work", action=action,
                          resource_type="contract", resource_id=c["id"], goal_id=c["goal_id"])


def _record_checks(ctx: Ctx, c: dict, results: list[dict]) -> None:
    known = set(c["acceptance_checks"])
    for r in results:
        if r["check"] not in known:
            raise Unprocessable(f"'{r['check']}' is not one of the contract's acceptance checks", code="UNKNOWN_CHECK")
        if r["check"].startswith("review:") and not ctx.is_human:
            raise policy.deny(ctx, f"'{r['check']}' is a review and must be signed off by a human validator",
                              action="contract.record_check", resource_type="contract", resource_id=c["id"],
                              goal_id=c["goal_id"])
        ctx.tx.execute(
            """INSERT INTO validation_results (tenant_id, id, contract_id, fencing_token, check_key, status, validator_id, evidence)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
            (ctx.tenant_id, new_id("check"), c["id"], c["fencing_token"], r["check"], r["status"], ctx.actor_id,
             r.get("evidence") or {}))


def check_status(tx: Tx, tenant_id: str, c: dict) -> dict[str, str | None]:
    """Latest recorded result per acceptance check for the current attempt."""
    rows = tx.all(
        """SELECT DISTINCT ON (check_key) check_key, status FROM validation_results
           WHERE tenant_id = %s AND contract_id = %s AND fencing_token = %s ORDER BY check_key, created_at DESC, id DESC""",
        (tenant_id, c["id"], c["fencing_token"]))
    latest = {r["check_key"]: r["status"] for r in rows}
    return {k: latest.get(k) for k in c["acceptance_checks"]}


def record_checks(ctx: Ctx, contract_id: str, results: list[dict]) -> dict:
    """Record check results without deciding the contract (used by automated validators)."""
    c = get(ctx.tx, ctx.tenant_id, contract_id, lock=True)
    _require_validator(ctx, c, "contract.record_check")
    if c["status"] == states.SUBMITTED:
        c = begin_validation(ctx, c)
    if c["status"] != states.VALIDATING:
        raise states.InvalidTransition(f"checks can only be recorded during validation, not '{c['status']}'")
    _record_checks(ctx, c, results)
    return c


def accept(ctx: Ctx, contract_id: str, results: list[dict], expected_version: int | None) -> dict:
    """validating -> accepted, only when every mandatory check passed and the pinned inputs are
    still current (spec §3, T14). Accepting publishes the attempt's artifacts and may supersede
    earlier versions, which invalidates work pinned to them."""
    c = get(ctx.tx, ctx.tenant_id, contract_id, lock=True)
    _require_validator(ctx, c, "contract.accept")
    check_version(c, expected_version, "contract")
    if c["status"] not in (states.SUBMITTED, states.VALIDATING):
        states.check_transition(c["status"], states.ACCEPTED)
    stale = stale_pins(ctx.tx, ctx.tenant_id, c)
    if c["stale_inputs"] or stale:
        raise Conflict("the contract's inputs were invalidated during validation; it cannot be accepted",
                       code="STALE_INPUT", details={"stale_pins": stale})
    pending_decision = ctx.tx.one(
        """SELECT id FROM decisions WHERE tenant_id = %s AND state IN ('open','awaiting_authority')
             AND kind <> 'authority' AND contract_ids @> %s::jsonb LIMIT 1""", (ctx.tenant_id, [c["id"]]))
    if pending_decision:
        raise Conflict("an open decision concerns this contract; it cannot be accepted until that is resolved",
                       code="DECISION_PENDING", details={"decision_id": pending_decision["id"]})
    if c["status"] == states.SUBMITTED:
        c = begin_validation(ctx, c)
    _record_checks(ctx, c, results)
    status = check_status(ctx.tx, ctx.tenant_id, c)
    failed = sorted(k for k, v in status.items() if v == "fail")
    pending = sorted(k for k, v in status.items() if v is None)
    if failed or pending:
        raise Unprocessable("every acceptance check must pass before a contract can be accepted",
                            code="CHECKS_INCOMPLETE", details={"failed": failed, "pending": pending})
    changes = artifacts.accept_attempt_versions(ctx, c)
    _fence(ctx, c, "accepted", "accepted")
    new = _transition(ctx, c, states.ACCEPTED,
                      payload={"validator_id": ctx.actor_id, "checks": status,
                               "artifacts": [f"{a}@{v}" for a, _, v, _ in changes]})
    accepted_event = ctx.event_ids[-1]
    for artifact_id, old, new_version, event_id in changes:
        if old:
            propagate_invalidation(ctx.caused_by(event_id), artifact_id, old, new_version)
    reconcile_goal(ctx.caused_by(accepted_event), c["goal_id"])
    return get(ctx.tx, ctx.tenant_id, new["id"])


def reject(ctx: Ctx, contract_id: str, reason: str, results: list[dict], expected_version: int | None) -> dict:
    """validating -> rejected. The rejected artifact versions and check results are preserved."""
    c = get(ctx.tx, ctx.tenant_id, contract_id, lock=True)
    _require_validator(ctx, c, "contract.reject")
    check_version(c, expected_version, "contract")
    if c["status"] not in (states.SUBMITTED, states.VALIDATING):
        states.check_transition(c["status"], states.REJECTED)
    if c["status"] == states.SUBMITTED:
        c = begin_validation(ctx, c)
    _record_checks(ctx, c, results)
    _fence(ctx, c, "rejected", reason or "validation_failed")
    return _transition(ctx, c, states.REJECTED, reason="validation_failed", stale_inputs=False,
                       payload={"validator_id": ctx.actor_id, "note": reason,
                                "checks": check_status(ctx.tx, ctx.tenant_id, c)})
