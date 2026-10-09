"""Conflicts and decision junctions (spec §7). Conflicts get a bounded number of automatic
reconciliation rounds; anything unresolved becomes a decision for a named human."""
from __future__ import annotations

from cas.config import settings
from cas.domain.errors import Conflict, NotFound, PolicyDenied, Unprocessable
from cas.persistence.db import Tx
from cas.service import contracts, policy
from cas.service.core import Ctx, audit, bump, check_version, emit, fingerprint, new_id
from cas.service.principals import require_principal

DECISION_KINDS = ("factual", "preference", "context", "authority", "other")
CONFLICT_KINDS = ("version_collision", "dependency_invalid", "artifact_incompatibility",
                  "evidence_disagreement", "priority_tradeoff", "permission_denied")
KIND_FOR_CONFLICT = {"evidence_disagreement": "factual", "priority_tradeoff": "preference",
                     "permission_denied": "authority"}
OPEN_STATES = ("open", "awaiting_authority")


# ---------------------------------------------------------------- decisions

def get(tx: Tx, tenant_id: str, decision_id: str, lock: bool = False) -> dict:
    row = tx.one(f"SELECT * FROM decisions WHERE tenant_id = %s AND id = %s{' FOR UPDATE' if lock else ''}",
                 (tenant_id, decision_id))
    if row is None:
        raise NotFound(f"decision '{decision_id}' not found")
    return row


def view(tx: Tx, d: dict) -> dict:
    out = dict(d)
    out["resolutions"] = tx.all(
        "SELECT * FROM decision_resolutions WHERE tenant_id = %s AND decision_id = %s ORDER BY created_at",
        (d["tenant_id"], d["id"]))
    out["baseline_current"] = snapshot(tx, d["tenant_id"], d["evidence_refs"]) == d["baseline"]
    return out


def snapshot(tx: Tx, tenant_id: str, evidence_refs: list[dict]) -> list[dict]:
    """The state of every referenced artifact version right now. A decision is made against this
    baseline; if it has moved by the time the decision is resolved, the resolution is refused."""
    out = []
    for ref in evidence_refs:
        if "artifact_id" not in ref:
            continue
        row = tx.one(
            """SELECT v.status, a.current_version_id FROM artifact_versions v JOIN artifacts a
                 ON a.tenant_id = v.tenant_id AND a.id = v.artifact_id
               WHERE v.tenant_id = %s AND v.artifact_id = %s AND v.version_id = %s""",
            (tenant_id, ref["artifact_id"], ref.get("version_id")))
        if row is None:
            raise Unprocessable(f"evidence '{ref['artifact_id']}@{ref.get('version_id')}' is not an artifact "
                                "version in this tenant", code="UNKNOWN_ARTIFACT")
        out.append({"artifact_id": ref["artifact_id"], "version_id": ref["version_id"], "status": row["status"],
                    "current_version_id": row["current_version_id"]})
    return out


def _descendants(tx: Tx, tenant_id: str, contract_ids: list[str]) -> list[str]:
    if not contract_ids:
        return []
    rows = tx.all(
        """WITH RECURSIVE down AS (
             SELECT downstream_id FROM contract_dependencies WHERE tenant_id = %(t)s AND upstream_id = ANY(%(ids)s::text[])
             UNION
             SELECT d.downstream_id FROM contract_dependencies d JOIN down ON d.upstream_id = down.downstream_id
              WHERE d.tenant_id = %(t)s)
           SELECT downstream_id FROM down""",
        {"t": tenant_id, "ids": "{" + ",".join(contract_ids) + "}"})
    return [r["downstream_id"] for r in rows]


def create(ctx: Ctx, body: dict, *, conflict_id: str | None = None, internal: bool = False) -> dict:
    goal_id = body["goal_id"]
    if not ctx.tx.one("SELECT 1 FROM goals WHERE tenant_id = %s AND id = %s", (ctx.tenant_id, goal_id)):
        raise Unprocessable(f"goal '{goal_id}' not found", code="UNKNOWN_GOAL")
    if not internal and not policy.roles(ctx.tx, ctx.tenant_id, ctx.actor_id, goal_id) and not _owns_goal(ctx, goal_id):
        raise policy.deny(ctx, "opening a decision requires a role on the goal", action="decision.create",
                          resource_type="goal", resource_id=goal_id, goal_id=goal_id)
    owner = require_principal(ctx.tx, ctx.tenant_id, body["decision_owner_id"], "decision_owner_id", kind="human")
    contract_ids = list(dict.fromkeys(body.get("contract_ids") or []))
    for cid in contract_ids:
        c = ctx.tx.one("SELECT goal_id FROM contracts WHERE tenant_id = %s AND id = %s", (ctx.tenant_id, cid))
        if c is None or c["goal_id"] != goal_id:
            raise Unprocessable(f"'{cid}' is not a contract of this goal", code="UNKNOWN_CONTRACT")
    alternatives = body["alternatives"]
    if len({a["id"] for a in alternatives}) != len(alternatives):
        raise Unprocessable("alternative ids must be unique", code="DUPLICATE_ALTERNATIVE")
    evidence = body.get("evidence_refs") or []
    held: list[str] = []
    if body.get("hold_dependents", True):
        # Hold the work that depends on the answer. Contracts named in the decision that are already
        # producing keep their proposals, which are the evidence being decided on, but cannot be
        # accepted while the decision is open (see contracts.accept).
        not_started = [r["id"] for r in ctx.tx.all(
            "SELECT id FROM contracts WHERE tenant_id = %s AND id = ANY(%s::text[]) AND status IN ('proposed','ready','blocked')",
            (ctx.tenant_id, "{" + ",".join(contract_ids) + "}"))]
        held = list(dict.fromkeys(not_started + _descendants(ctx.tx, ctx.tenant_id, contract_ids)))
    kind = body.get("kind", "other")
    d = ctx.tx.one(
        """INSERT INTO decisions (tenant_id, id, goal_id, contract_ids, held_contract_ids, question, kind, alternatives,
               evidence_refs, baseline, consequences, decision_owner_id, state, conflict_id, created_by)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *""",
        (ctx.tenant_id, new_id("decision"), goal_id, contract_ids, held, body["question"], kind, alternatives,
         evidence, snapshot(ctx.tx, ctx.tenant_id, evidence), body.get("consequences", ""), owner["id"],
         "awaiting_authority" if kind == "authority" else "open", conflict_id, ctx.actor_id))
    evt = emit(ctx, "cas.decision.opened.v1", "decision", d["id"], d["version"], goal_id,
               {"question": d["question"], "kind": kind, "decision_owner_id": owner["id"],
                "contract_ids": contract_ids, "held_contract_ids": held, "conflict_id": conflict_id})
    cause = ctx.caused_by(evt)
    for cid in held:
        contracts.hold_for(cause, cid, "decision_hold", {"decision_id": d["id"]})
    return d


def _owns_goal(ctx: Ctx, goal_id: str) -> bool:
    row = ctx.tx.one("SELECT owner_principal_id FROM goals WHERE tenant_id = %s AND id = %s", (ctx.tenant_id, goal_id))
    return bool(row) and row["owner_principal_id"] == ctx.actor_id


def _require_decision_owner(ctx: Ctx, d: dict, action: str) -> None:
    if ctx.actor_id != d["decision_owner_id"] or not ctx.is_human:
        raise policy.deny(ctx, "only the decision's owner may do this", action=action, resource_type="decision",
                          resource_id=d["id"], goal_id=d["goal_id"])


def _release(ctx: Ctx, d: dict) -> None:
    """Dependency readiness and reconciliation after a decision closes (spec §7)."""
    for cid in d["held_contract_ids"]:
        contracts.reconcile_one(ctx, cid)


def resolve(ctx: Ctx, decision_id: str, body: dict, expected_version: int | None) -> dict:
    """Record the owner's choice. This changes no permission and waives no validation gate; it
    only lifts the holds this decision placed (spec §2, §7)."""
    d = get(ctx.tx, ctx.tenant_id, decision_id, lock=True)
    _require_decision_owner(ctx, d, "decision.resolve")
    check_version(d, expected_version, "decision")
    if d["state"] not in OPEN_STATES:
        raise Conflict(f"this decision is already {d['state']}", code="DECISION_NOT_OPEN")
    if body["chosen_alternative"] not in {a["id"] for a in d["alternatives"]}:
        raise Unprocessable("chosen_alternative is not one of the decision's alternatives", code="UNKNOWN_ALTERNATIVE")
    now = snapshot(ctx.tx, ctx.tenant_id, d["evidence_refs"])
    if now != d["baseline"]:
        changed = [n for n, b in zip(now, d["baseline"]) if n != b]
        raise Conflict("evidence this decision refers to has changed since it was opened; review the current "
                       "versions and refresh the decision before resolving it",
                       code="DECISION_BASELINE_CHANGED", details={"changed": changed, "baseline": d["baseline"]})
    resolution = {"chosen_alternative": body["chosen_alternative"], "rationale": body["rationale"],
                  "actor_id": ctx.actor_id, "input_versions": now, "impacted_contract_ids": d["held_contract_ids"]}
    ctx.tx.execute(
        """INSERT INTO decision_resolutions (tenant_id, id, decision_id, actor_id, chosen_alternative, rationale,
               input_versions, impacted_contract_ids) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
        (ctx.tenant_id, new_id("dres"), d["id"], ctx.actor_id, body["chosen_alternative"], body["rationale"], now,
         d["held_contract_ids"]))
    new = bump(ctx.tx, "decisions", ctx.tenant_id, d["id"], state="resolved", resolution=resolution)
    evt = emit(ctx, "cas.decision.resolved.v1", "decision", d["id"], new["version"], d["goal_id"], resolution)
    cause = ctx.caused_by(evt)
    if d["conflict_id"]:
        cf = bump(ctx.tx, "conflicts", ctx.tenant_id, d["conflict_id"], status="resolved",
                  resolution={"decision_id": d["id"], "chosen_alternative": body["chosen_alternative"]})
        emit(cause, "cas.conflict.resolved.v1", "conflict", cf["id"], cf["version"], cf["goal_id"],
             {"decision_id": d["id"]})
    _release(cause, new)
    return new


def refresh(ctx: Ctx, decision_id: str, body: dict, expected_version: int | None) -> dict:
    """Re-baseline an open decision against current evidence, optionally replacing the evidence."""
    d = get(ctx.tx, ctx.tenant_id, decision_id, lock=True)
    if ctx.actor_id not in (d["decision_owner_id"], d["created_by"]):
        raise policy.deny(ctx, "only the decision's owner or creator may refresh it", action="decision.refresh",
                          resource_type="decision", resource_id=d["id"], goal_id=d["goal_id"])
    check_version(d, expected_version, "decision")
    if d["state"] not in OPEN_STATES:
        raise Conflict(f"this decision is already {d['state']}", code="DECISION_NOT_OPEN")
    evidence = body.get("evidence_refs") or d["evidence_refs"]
    new = bump(ctx.tx, "decisions", ctx.tenant_id, d["id"], evidence_refs=evidence,
               baseline=snapshot(ctx.tx, ctx.tenant_id, evidence))
    emit(ctx, "cas.decision.rebaselined.v1", "decision", d["id"], new["version"], d["goal_id"],
         {"before": d["baseline"], "after": new["baseline"]})
    return new


def cancel(ctx: Ctx, decision_id: str, note: str, expected_version: int | None) -> dict:
    d = get(ctx.tx, ctx.tenant_id, decision_id, lock=True)
    _require_decision_owner(ctx, d, "decision.cancel")
    check_version(d, expected_version, "decision")
    if d["state"] not in OPEN_STATES:
        raise Conflict(f"this decision is already {d['state']}", code="DECISION_NOT_OPEN")
    new = bump(ctx.tx, "decisions", ctx.tenant_id, d["id"], state="cancelled")
    evt = emit(ctx, "cas.decision.cancelled.v1", "decision", d["id"], new["version"], d["goal_id"], {"note": note})
    _release(ctx.caused_by(evt), new)
    return new


# ---------------------------------------------------------------- conflicts

def get_conflict(tx: Tx, tenant_id: str, conflict_id: str, lock: bool = False) -> dict:
    row = tx.one(f"SELECT * FROM conflicts WHERE tenant_id = %s AND id = %s{' FOR UPDATE' if lock else ''}",
                 (tenant_id, conflict_id))
    if row is None:
        raise NotFound(f"conflict '{conflict_id}' not found")
    return row


def _proposal_hashes(tx: Tx, tenant_id: str, proposals: list[dict]) -> list[str]:
    hashes = []
    for p in proposals:
        row = tx.one("SELECT sha256 FROM artifact_versions WHERE tenant_id = %s AND artifact_id = %s AND version_id = %s",
                     (tenant_id, p["artifact_id"], p["version_id"]))
        if row is None:
            raise Unprocessable(f"proposal '{p['artifact_id']}@{p['version_id']}' is not an artifact version in this tenant",
                                code="UNKNOWN_ARTIFACT")
        hashes.append(row["sha256"])
    return hashes


def _auto_resolution(tx: Tx, tenant_id: str, proposals: list[dict]) -> dict | None:
    """Preapproved deterministic policy: proposals with identical content are not in conflict.
    Agent votes are never counted as evidence (spec §7)."""
    hashes = _proposal_hashes(tx, tenant_id, proposals)
    if len(hashes) >= 2 and len(set(hashes)) == 1:
        return {"policy": "identical_content_hash", "sha256": hashes[0]}
    return None


def open_conflict(ctx: Ctx, body: dict) -> dict:
    goal_id = body["goal_id"]
    if not ctx.tx.one("SELECT 1 FROM goals WHERE tenant_id = %s AND id = %s", (ctx.tenant_id, goal_id)):
        raise Unprocessable(f"goal '{goal_id}' not found", code="UNKNOWN_GOAL")
    if not policy.roles(ctx.tx, ctx.tenant_id, ctx.actor_id, goal_id) and not _owns_goal(ctx, goal_id):
        raise policy.deny(ctx, "reporting a conflict requires a role on the goal", action="conflict.create",
                          resource_type="goal", resource_id=goal_id, goal_id=goal_id)
    contract_ids = list(dict.fromkeys(body.get("contract_ids") or []))
    rows = []
    for cid in contract_ids:
        c = ctx.tx.one("SELECT goal_id, escalation_policy FROM contracts WHERE tenant_id = %s AND id = %s",
                       (ctx.tenant_id, cid))
        if c is None or c["goal_id"] != goal_id:
            raise Unprocessable(f"'{cid}' is not a contract of this goal", code="UNKNOWN_CONTRACT")
        rows.append(c)
    proposals = body.get("proposals") or []
    resolution = _auto_resolution(ctx.tx, ctx.tenant_id, proposals) if body["kind"] != "permission_denied" else None
    cf = ctx.tx.one(
        """INSERT INTO conflicts (tenant_id, id, goal_id, kind, contract_ids, proposals, description, max_rounds,
               evidence_fingerprints, status, resolution, created_by)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *""",
        (ctx.tenant_id, new_id("conflict"), goal_id, body["kind"], contract_ids, proposals,
         body.get("description", ""), settings.conflict_max_rounds,
         [fingerprint({"proposals": proposals, "evidence_refs": body.get("evidence_refs") or []})],
         "auto_resolved" if resolution else "open", resolution, ctx.actor_id))
    evt = emit(ctx, f"cas.conflict.{'auto_resolved' if resolution else 'opened'}.v1", "conflict", cf["id"],
               cf["version"], goal_id, {"kind": cf["kind"], "contract_ids": contract_ids, "proposals": proposals,
                                        "resolution": resolution})
    if resolution:
        return cf
    # Permission denials are never negotiated, and a contract may opt out of negotiation entirely.
    if cf["kind"] == "permission_denied" or any(r["escalation_policy"]["on_conflict"] == "escalate" for r in rows):
        return escalate(ctx.caused_by(evt), cf, body)
    return cf


def add_round(ctx: Ctx, conflict_id: str, body: dict, expected_version: int | None) -> dict:
    """One autonomous reconciliation round. A round must bring a changed artifact or evidence
    reference, and the pilot allows two rounds; the next one escalates to a human (T12)."""
    cf = get_conflict(ctx.tx, ctx.tenant_id, conflict_id, lock=True)
    if not policy.roles(ctx.tx, ctx.tenant_id, ctx.actor_id, cf["goal_id"]) and not _owns_goal(ctx, cf["goal_id"]):
        raise policy.deny(ctx, "negotiating a conflict requires a role on the goal", action="conflict.round",
                          resource_type="conflict", resource_id=cf["id"], goal_id=cf["goal_id"])
    check_version(cf, expected_version, "conflict")
    if cf["status"] != "open":
        raise Conflict(f"this conflict is already {cf['status']}", code="CONFLICT_NOT_OPEN")
    proposals = body.get("proposals") or cf["proposals"]
    fp = fingerprint({"proposals": proposals, "evidence_refs": body.get("evidence_refs") or []})
    if fp in cf["evidence_fingerprints"]:
        raise Unprocessable("a new round needs a changed artifact or evidence reference", code="NO_NEW_EVIDENCE")
    if cf["rounds"] >= cf["max_rounds"]:
        # Out of rounds: the human decides between the latest proposals on the table.
        cf = bump(ctx.tx, "conflicts", ctx.tenant_id, cf["id"], proposals=proposals,
                  evidence_fingerprints=cf["evidence_fingerprints"] + [fp])
        return escalate(ctx, cf, body)
    resolution = _auto_resolution(ctx.tx, ctx.tenant_id, proposals)
    new = bump(ctx.tx, "conflicts", ctx.tenant_id, cf["id"], rounds=cf["rounds"] + 1, proposals=proposals,
               evidence_fingerprints=cf["evidence_fingerprints"] + [fp],
               status="auto_resolved" if resolution else "open", resolution=resolution)
    emit(ctx, f"cas.conflict.{'auto_resolved' if resolution else 'round'}.v1", "conflict", cf["id"], new["version"],
         cf["goal_id"], {"round": new["rounds"], "max_rounds": new["max_rounds"], "proposals": proposals,
                         "note": body.get("note", ""), "resolution": resolution})
    return new


def escalate(ctx: Ctx, cf: dict, body: dict | None = None) -> dict:
    """Turn an unresolved conflict into a decision junction and hold the dependent work."""
    body = body or {}
    owner = body.get("decision_owner_id")
    if not owner and cf["contract_ids"]:
        owner = ctx.tx.scalar("SELECT escalation_policy->>'decision_owner_id' FROM contracts WHERE tenant_id = %s AND id = %s",
                              (ctx.tenant_id, cf["contract_ids"][0]))
    if not owner:
        owner = ctx.tx.scalar("SELECT owner_principal_id FROM goals WHERE tenant_id = %s AND id = %s",
                              (ctx.tenant_id, cf["goal_id"]))
    alternatives = body.get("alternatives") or [
        {"id": f"{p['artifact_id']}@{p['version_id']}", "label": f"Adopt {p['artifact_id']} {p['version_id']}"}
        for p in cf["proposals"]] or [{"id": "proceed", "label": "Proceed"}, {"id": "stop", "label": "Stop this work"}]
    d = create(ctx, {
        "goal_id": cf["goal_id"], "contract_ids": cf["contract_ids"],
        "question": body.get("question") or cf["description"] or f"Resolve {cf['kind'].replace('_', ' ')}",
        "kind": KIND_FOR_CONFLICT.get(cf["kind"], "context"), "alternatives": alternatives,
        "evidence_refs": [{"artifact_id": p["artifact_id"], "version_id": p["version_id"]} for p in cf["proposals"]],
        "consequences": body.get("consequences", ""), "decision_owner_id": owner,
        "hold_dependents": cf["kind"] != "permission_denied",
    }, conflict_id=cf["id"], internal=True)
    new = bump(ctx.tx, "conflicts", ctx.tenant_id, cf["id"], status="escalated", decision_id=d["id"])
    emit(ctx, "cas.conflict.escalated.v1", "conflict", cf["id"], new["version"], cf["goal_id"],
         {"decision_id": d["id"], "rounds": new["rounds"], "max_rounds": new["max_rounds"]})
    return new


def escalate_command(ctx: Ctx, conflict_id: str, body: dict, expected_version: int | None) -> dict:
    cf = get_conflict(ctx.tx, ctx.tenant_id, conflict_id, lock=True)
    if not policy.roles(ctx.tx, ctx.tenant_id, ctx.actor_id, cf["goal_id"]) and not _owns_goal(ctx, cf["goal_id"]):
        raise policy.deny(ctx, "escalating a conflict requires a role on the goal", action="conflict.escalate",
                          resource_type="conflict", resource_id=cf["id"], goal_id=cf["goal_id"])
    check_version(cf, expected_version, "conflict")
    if cf["status"] != "open":
        raise Conflict(f"this conflict is already {cf['status']}", code="CONFLICT_NOT_OPEN")
    return escalate(ctx, cf, body)


# ---------------------------------------------------------------- policy denials

def record_denial(ctx: Ctx, err: PolicyDenied) -> None:
    """Runs in its own transaction after a denied command rolled back: the denial is audited, shown
    on the goal's event stream, and, for side-effect boundaries, escalated to the contract's
    decision owner. That decision can never grant the permission (spec §7, §8, T09)."""
    audit_id = audit(ctx.tx, ctx.tenant_id, ctx.actor_id, err.action or "unknown", err.resource_type or "unknown",
                     err.resource_id, "deny", goal_id=err.goal_id, reason=err.message, details=err.details,
                     request_id=ctx.request_id)
    if not err.goal_id:
        return
    evt = emit(ctx, "cas.policy.denied.v1", "audit_action", audit_id, 1, err.goal_id,
               {"action": err.action, "resource_type": err.resource_type, "resource_id": err.resource_id,
                "principal_id": ctx.actor_id, "reason": err.message, "escalated": err.escalate, **err.details})
    if not (err.escalate and err.resource_type == "contract" and err.resource_id):
        return
    description = f"Permission denied: {err.action} needs scope '{err.details.get('scope', '?')}'"
    if ctx.tx.one(
            """SELECT 1 FROM conflicts WHERE tenant_id = %s AND kind = 'permission_denied' AND status = 'escalated'
                 AND contract_ids @> %s::jsonb AND description = %s""",
            (ctx.tenant_id, [err.resource_id], description)):
        return
    cf = ctx.tx.one(
        """INSERT INTO conflicts (tenant_id, id, goal_id, kind, contract_ids, description, status, created_by)
           VALUES (%s,%s,%s,'permission_denied',%s,%s,'open',%s) RETURNING *""",
        (ctx.tenant_id, new_id("conflict"), err.goal_id, [err.resource_id], description, ctx.actor_id))
    cause = ctx.caused_by(evt)
    emit(cause, "cas.conflict.opened.v1", "conflict", cf["id"], cf["version"], err.goal_id,
         {"kind": "permission_denied", "contract_ids": [err.resource_id]})
    escalate(cause, cf, {
        "question": f"{description}. {err.message}",
        "consequences": "Resolving this decision does not grant the permission. Access changes only through "
                        "a separate grant made by an admin.",
        "alternatives": [{"id": "keep_denied", "label": "Keep the action denied"},
                         {"id": "request_grant", "label": "Ask an admin to grant the scope separately"},
                         {"id": "revise_contract", "label": "Revise or cancel the contract"}],
    })
