"""Read models for the goal workspace (spec §5 overview/exceptions/events, §11 UI)."""
from __future__ import annotations

from cas.domain.errors import NotFound, Unprocessable
from cas.persistence.db import Tx
from cas.service import artifacts, contracts, decisions, effects, goals
from cas.service.core import to_cloudevent


def goal_events(tx: Tx, tenant_id: str, goal_id: str, cursor: str | None, limit: int, subject: str | None = None) -> dict:
    goals.get(tx, tenant_id, goal_id)
    try:
        after = int(cursor) if cursor else 0
    except ValueError:
        raise Unprocessable("cursor is not valid", code="INVALID_CURSOR") from None
    rows = tx.all(
        """SELECT * FROM events WHERE tenant_id = %(t)s AND goal_id = %(g)s AND seq > %(after)s
             AND (%(subject)s::text IS NULL OR subject = %(subject)s)
           ORDER BY seq LIMIT %(limit)s""",
        {"t": tenant_id, "g": goal_id, "after": after, "subject": subject, "limit": limit + 1})
    page = rows[:limit]
    return {"items": [to_cloudevent(r) for r in page],
            "next_cursor": str(page[-1]["seq"]) if page else (cursor or "0"),
            "has_more": len(rows) > limit}


def get_event(tx: Tx, tenant_id: str, event_id: str) -> dict:
    """One event with its provenance: what caused it and what it caused (the change inspector)."""
    row = tx.one("SELECT * FROM events WHERE tenant_id = %s AND id = %s", (tenant_id, event_id))
    if row is None:
        raise NotFound(f"event '{event_id}' not found")
    out = to_cloudevent(row)
    cause = tx.one("SELECT * FROM events WHERE tenant_id = %s AND id = %s", (tenant_id, row["causation_id"]))
    out["caused_by"] = to_cloudevent(cause) if cause else None
    out["caused"] = [to_cloudevent(r) for r in tx.all(
        "SELECT * FROM events WHERE tenant_id = %s AND causation_id = %s ORDER BY seq", (tenant_id, event_id))]
    out["same_request"] = [{"id": r["id"], "type": r["type"], "subject": r["subject"]} for r in tx.all(
        "SELECT id, type, subject FROM events WHERE tenant_id = %s AND correlation_id = %s ORDER BY seq",
        (tenant_id, row["correlation_id"]))]
    return out


def contract_detail(tx: Tx, tenant_id: str, contract_id: str) -> dict:
    c = contracts.get(tx, tenant_id, contract_id)
    out = contracts.view(tx, c)
    out["dependents"] = [r["downstream_id"] for r in tx.all(
        "SELECT downstream_id FROM contract_dependencies WHERE tenant_id = %s AND upstream_id = %s", (tenant_id, contract_id))]
    out["artifacts"] = artifacts.versions_for_contract(tx, tenant_id, contract_id)
    out["attempts"] = tx.all(
        "SELECT * FROM contract_attempts WHERE tenant_id = %s AND contract_id = %s ORDER BY fencing_token", (tenant_id, contract_id))
    out["checks"] = contracts.check_status(tx, tenant_id, c)
    out["check_history"] = tx.all(
        "SELECT * FROM validation_results WHERE tenant_id = %s AND contract_id = %s ORDER BY created_at", (tenant_id, contract_id))
    out["effects"] = effects.list_for_contract(tx, tenant_id, contract_id)
    out["revisions"] = tx.all(
        "SELECT * FROM contract_revisions WHERE tenant_id = %s AND contract_id = %s ORDER BY created_at", (tenant_id, contract_id))
    ok, reason, details = contracts.readiness(tx, tenant_id, c)
    out["readiness"] = {"ready": ok, "reason": reason, **{k: v for k, v in details.items() if k != "pins"}}
    return out


def overview(tx: Tx, tenant_id: str, goal_id: str) -> dict:
    goal = goals.get(tx, tenant_id, goal_id)
    rows = tx.all("SELECT * FROM contracts WHERE tenant_id = %s AND goal_id = %s ORDER BY created_at, id", (tenant_id, goal_id))
    names = {p["id"]: p["display_name"] for p in tx.all("SELECT id, display_name FROM principals WHERE tenant_id = %s", (tenant_id,))}
    versions = tx.all(
        """SELECT v.artifact_id, v.version_id, v.status, v.contract_id, v.sha256, a.artifact_type
           FROM artifact_versions v JOIN artifacts a ON a.tenant_id = v.tenant_id AND a.id = v.artifact_id
           WHERE v.tenant_id = %s AND a.goal_id = %s ORDER BY v.artifact_id, v.version_no""", (tenant_id, goal_id))
    by_contract: dict[str, list[dict]] = {}
    for v in versions:
        by_contract.setdefault(v["contract_id"], []).append(v)
    items = []
    for c in rows:
        item = contracts.view(tx, c)
        item["owner_name"] = names.get(c["owner_id"] or c["assigned_owner_id"])
        item["requester_name"] = names.get(c["requester_id"])
        item["artifacts"] = by_contract.get(c["id"], [])
        item["checks"] = contracts.check_status(tx, tenant_id, c)
        items.append(item)
    counts: dict[str, int] = {}
    for c in rows:
        counts[c["status"]] = counts.get(c["status"], 0) + 1
    return {
        "goal": dict(goal, owner_name=names.get(goal["owner_principal_id"])),
        "contract_counts": counts,
        "contracts": items,
        "goal_inputs": [v for v in versions if v["contract_id"] is None],
        "pending_revisions": tx.all(
            "SELECT * FROM goal_revisions WHERE tenant_id = %s AND goal_id = %s AND status = 'proposed' ORDER BY created_at",
            (tenant_id, goal_id)),
        "exception_count": len(exceptions(tx, tenant_id, goal_id)["items"]),
        "metrics": {r["metric"]: r["value"] for r in tx.all(
            "SELECT metric, value FROM metrics_counters WHERE tenant_id = %s AND goal_id = %s", (tenant_id, goal_id))},
    }


def exceptions(tx: Tx, tenant_id: str, goal_id: str) -> dict:
    """Everything on the goal that needs a human: computed from current state, so reading or
    summarising a notification can never make an unresolved exception disappear (spec §8)."""
    goals.get(tx, tenant_id, goal_id)
    items: list[dict] = []
    for c in tx.all(
            """SELECT * FROM contracts WHERE tenant_id = %s AND goal_id = %s
                 AND (status IN ('blocked','failed','rejected') OR requires_reassessment OR stale_inputs)
                 AND status <> 'cancelled' ORDER BY updated_at""", (tenant_id, goal_id)):
        if c["status"] == "blocked":
            kind = {"stale_input": "invalidated", "policy_denied": "permission_denied",
                    "decision_hold": "awaiting_decision"}.get(c["blocked_reason"], "blocked")
        elif c["status"] in ("failed", "rejected"):
            kind = c["status"]
        elif c["stale_inputs"]:
            kind = "invalidated"
        else:
            kind = "requires_reassessment"
        actions = {"blocked": ["revalidate", "revise", "cancel"], "failed": ["retry", "cancel"],
                   "rejected": ["retry", "cancel"]}.get(c["status"], ["revise"])
        items.append({"kind": kind, "contract_id": c["id"], "title": c["title"], "status": c["status"],
                      "reason": c["blocked_reason"] or c["status_reason"], "since": c["updated_at"],
                      "attempt": c["attempt"], "max_attempts": c["max_attempts"], "actions": actions})
    for d in tx.all(
            "SELECT * FROM decisions WHERE tenant_id = %s AND goal_id = %s AND state IN ('open','awaiting_authority') ORDER BY created_at",
            (tenant_id, goal_id)):
        v = decisions.view(tx, d)
        items.append({"kind": "permission_denied" if d["kind"] == "authority" else "decision", "decision_id": d["id"],
                      "title": d["question"], "status": d["state"], "decision_owner_id": d["decision_owner_id"],
                      "alternatives": d["alternatives"], "held_contract_ids": d["held_contract_ids"],
                      "consequences": d["consequences"], "baseline_current": v["baseline_current"],
                      "version": d["version"], "since": d["created_at"], "actions": ["resolve"]})
    for cf in tx.all("SELECT * FROM conflicts WHERE tenant_id = %s AND goal_id = %s AND status = 'open' ORDER BY created_at",
                     (tenant_id, goal_id)):
        items.append({"kind": "unresolved_conflict", "conflict_id": cf["id"], "title": cf["description"] or cf["kind"],
                      "status": f"round {cf['rounds']} of {cf['max_rounds']}", "contract_ids": cf["contract_ids"],
                      "since": cf["created_at"], "actions": ["escalate"]})
    for e in tx.all(
            """SELECT e.*, c.title FROM external_effects e JOIN contracts c ON c.tenant_id = e.tenant_id AND c.id = e.contract_id
               WHERE e.tenant_id = %s AND c.goal_id = %s AND e.status = 'manual_recovery_required' ORDER BY e.updated_at""",
            (tenant_id, goal_id)):
        items.append({"kind": "manual_recovery", "effect_id": e["id"], "contract_id": e["contract_id"],
                      "title": f"Step '{e['step_key']}' of '{e['title']}'", "status": e["status"],
                      "reason": f"scope {e['scope']}, not idempotent", "since": e["updated_at"], "actions": ["recover"]})
    for r in tx.all("SELECT * FROM goal_revisions WHERE tenant_id = %s AND goal_id = %s AND status = 'proposed' ORDER BY created_at",
                    (tenant_id, goal_id)):
        items.append({"kind": "goal_revision_proposed", "revision_id": r["id"], "title": r["rationale"] or "Proposed goal revision",
                      "status": "awaiting owner approval", "proposed_by": r["proposed_by"], "changes": r["changes"],
                      "base_version": r["base_version"], "since": r["created_at"], "actions": ["approve", "reject"]})
    return {"items": items}
