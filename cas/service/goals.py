"""Goal contracts and their revisions (spec §2 GoalContract, §8)."""
from __future__ import annotations

from cas.domain import schemas, states
from cas.domain.errors import Conflict, NotFound, Unprocessable
from cas.persistence.db import Tx
from cas.service import contracts, policy
from cas.service.core import Ctx, bump, check_version, emit, new_id
from cas.service.principals import require_principal

GOAL_FIELDS = ("title", "objective", "success_criteria", "constraints", "preferences")
# Changing these changes what the work is for, so active contracts need another look.
SCOPE_FIELDS = frozenset({"objective", "success_criteria", "constraints"})


def get(tx: Tx, tenant_id: str, goal_id: str, lock: bool = False) -> dict:
    row = tx.one(f"SELECT * FROM goals WHERE tenant_id = %s AND id = %s{' FOR UPDATE' if lock else ''}",
                 (tenant_id, goal_id))
    if row is None:
        raise NotFound(f"goal '{goal_id}' not found")
    return row


def list_goals(tx: Tx, tenant_id: str) -> list[dict]:
    return tx.all("SELECT * FROM goals WHERE tenant_id = %s ORDER BY created_at DESC", (tenant_id,))


def _owns(ctx: Ctx, goal: dict) -> bool:
    return ctx.is_human and goal["owner_principal_id"] == ctx.actor_id


def _require_owner(ctx: Ctx, goal: dict, action: str) -> None:
    if not _owns(ctx, goal):
        raise policy.deny(ctx, "only the goal's human owner may do this", action=action, resource_type="goal",
                          resource_id=goal["id"], goal_id=goal["id"])


def create(ctx: Ctx, body: dict) -> dict:
    schemas.validate("goal", body)
    if not (policy.is_admin(ctx) or (ctx.is_human and policy.has_role(ctx.tx, ctx.tenant_id, ctx.actor_id, "goal_owner"))):
        raise policy.deny(ctx, "creating a goal requires a human with the 'goal_owner' or 'admin' role",
                          action="goal.create", resource_type="goal")
    owner = require_principal(ctx.tx, ctx.tenant_id, body["owner_principal_id"], "owner_principal_id", kind="human")
    if owner["id"] != ctx.actor_id and not policy.is_admin(ctx):
        raise policy.deny(ctx, "only an admin may create a goal owned by someone else", action="goal.create",
                          resource_type="goal")
    row = ctx.tx.one(
        """INSERT INTO goals (tenant_id, id, title, objective, success_criteria, constraints, preferences, owner_principal_id)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *""",
        (ctx.tenant_id, new_id("goal"), body["title"], body["objective"], body["success_criteria"],
         body["constraints"], body["preferences"], owner["id"]))
    emit(ctx, "cas.goal.created.v1", "goal", row["id"], row["version"], row["id"],
         {"title": row["title"], "owner_principal_id": owner["id"], "status": "draft"})
    return row


def set_status(ctx: Ctx, goal_id: str, status: str, expected_version: int | None) -> dict:
    goal = get(ctx.tx, ctx.tenant_id, goal_id, lock=True)
    _require_owner(ctx, goal, f"goal.{status}")
    check_version(goal, expected_version, "goal")
    states.check_goal_transition(goal["status"], status)
    if status == "completed":
        open_work = ctx.tx.scalar(
            "SELECT count(*) FROM contracts WHERE tenant_id = %s AND goal_id = %s AND status NOT IN ('accepted','cancelled')",
            (ctx.tenant_id, goal_id))
        if open_work:
            raise Unprocessable(f"{open_work} contract(s) are still open; accept or cancel them first",
                                code="GOAL_HAS_OPEN_WORK")
    new = bump(ctx.tx, "goals", ctx.tenant_id, goal_id, status=status)
    name = "activated" if status == "active" else status
    evt = emit(ctx, f"cas.goal.{name}.v1", "goal", goal_id, new["version"], goal_id,
               {"from_status": goal["status"], "to_status": status})
    cause = ctx.caused_by(evt)
    if status == "active":
        contracts.reconcile_goal(cause, goal_id)
    elif status == "cancelled":
        for r in ctx.tx.all(
                "SELECT id FROM contracts WHERE tenant_id = %s AND goal_id = %s AND status NOT IN ('accepted','cancelled') FOR UPDATE",
                (ctx.tenant_id, goal_id)):
            contracts.cancel_internal(cause, contracts.get(ctx.tx, ctx.tenant_id, r["id"]), "goal cancelled")
    return new


def _apply(ctx: Ctx, goal: dict, changes: dict, hold_ids: list[str], revision_id: str, proposed_by: str) -> dict:
    """Write a new goal version and run impact analysis on its contracts (spec §2, §4)."""
    doc = {k: goal[k] for k in GOAL_FIELDS} | changes | {"owner_principal_id": goal["owner_principal_id"]}
    schemas.validate("goal", doc)
    before = {k: goal[k] for k in changes}
    new = bump(ctx.tx, "goals", ctx.tenant_id, goal["id"], **changes)
    scope_changed = bool(SCOPE_FIELDS & set(changes))
    evt = emit(ctx, "cas.goal.revised.v1", "goal", goal["id"], new["version"], goal["id"],
               {"revision_id": revision_id, "proposed_by": proposed_by, "approved_by": ctx.actor_id,
                "before": before, "after": {k: new[k] for k in changes}, "scope_changed": scope_changed})
    cause = ctx.caused_by(evt)
    held, flagged = [], []
    for cid in hold_ids:
        c = ctx.tx.one("SELECT id FROM contracts WHERE tenant_id = %s AND id = %s AND goal_id = %s",
                       (ctx.tenant_id, cid, goal["id"]))
        if c is None:
            raise Unprocessable(f"'{cid}' is not a contract of this goal", code="UNKNOWN_CONTRACT")
        if contracts.hold_for(cause, cid, "goal_revised", {"goal_version": new["version"], "revision_id": revision_id}):
            held.append(cid)
    if scope_changed:
        for r in ctx.tx.all(
                """SELECT id FROM contracts WHERE tenant_id = %s AND goal_id = %s AND NOT requires_reassessment
                     AND status IN ('ready','leased','running','submitted','validating','accepted') FOR UPDATE""",
                (ctx.tenant_id, goal["id"])):
            c = bump(ctx.tx, "contracts", ctx.tenant_id, r["id"], requires_reassessment=True)
            emit(cause, "cas.contract.reassessment_required.v1", "contract", c["id"], c["version"], goal["id"],
                 {"reason": "goal_revised", "goal_version": new["version"], "revision_id": revision_id})
            flagged.append(c["id"])
    ctx.tx.execute(
        """UPDATE goal_revisions SET status = 'applied', decided_by = %s, decided_at = now(), resulting_version = %s,
                  before = %s, updated_at = now(), version = version + 1 WHERE tenant_id = %s AND id = %s""",
        (ctx.actor_id, new["version"], before, ctx.tenant_id, revision_id))
    return dict(new, impact={"held_contract_ids": held, "reassessment_contract_ids": flagged})


def _clean_changes(body: dict) -> dict:
    changes = body.get("changes") or {}
    unknown = sorted(set(changes) - set(GOAL_FIELDS))
    if unknown:
        raise Unprocessable(f"these goal fields cannot be revised: {', '.join(unknown)}", code="FIELD_NOT_REVISABLE")
    if not changes:
        raise Unprocessable("changes is empty", code="NO_CHANGES")
    return changes


def revise(ctx: Ctx, goal_id: str, body: dict, expected_version: int | None) -> dict:
    """The goal's human owner revises it directly. Anyone else, agents included, creates a
    proposal that changes nothing until the owner approves it (spec §8, T10)."""
    goal = get(ctx.tx, ctx.tenant_id, goal_id, lock=True)
    changes = _clean_changes(body)
    doc = {k: goal[k] for k in GOAL_FIELDS} | changes | {"owner_principal_id": goal["owner_principal_id"]}
    schemas.validate("goal", doc)
    if goal["status"] in ("completed", "cancelled"):
        raise Conflict(f"a {goal['status']} goal cannot be revised", code="GOAL_CLOSED")
    owner = _owns(ctx, goal)
    if owner:
        if expected_version is None:
            from cas.domain.errors import PreconditionRequired
            raise PreconditionRequired("If-Match with the goal's version is required to revise it")
        check_version(goal, expected_version, "goal")
    elif not policy.roles(ctx.tx, ctx.tenant_id, ctx.actor_id, goal_id):
        raise policy.deny(ctx, "proposing a goal revision requires a role on the goal", action="goal.propose_revision",
                          resource_type="goal", resource_id=goal_id, goal_id=goal_id)
    rev = ctx.tx.one(
        """INSERT INTO goal_revisions (tenant_id, id, goal_id, base_version, proposed_by, status, changes, rationale, hold_contract_ids)
           VALUES (%s,%s,%s,%s,%s,'proposed',%s,%s,%s) RETURNING *""",
        (ctx.tenant_id, new_id("grev"), goal_id, goal["version"], ctx.actor_id, changes, body.get("rationale", ""),
         body.get("hold_contract_ids") or []))
    if owner:
        new = _apply(ctx, goal, changes, rev["hold_contract_ids"], rev["id"], ctx.actor_id)
        return {"goal": new, "revision": get_revision(ctx.tx, ctx.tenant_id, rev["id"])}
    emit(ctx, "cas.goal.revision_proposed.v1", "goal_revision", rev["id"], rev["version"], goal_id,
         {"goal_id": goal_id, "proposed_by": ctx.actor_id, "base_version": goal["version"], "changes": changes,
          "rationale": rev["rationale"]})
    return {"goal": goal, "revision": rev}


def get_revision(tx: Tx, tenant_id: str, revision_id: str, lock: bool = False) -> dict:
    row = tx.one(f"SELECT * FROM goal_revisions WHERE tenant_id = %s AND id = %s{' FOR UPDATE' if lock else ''}",
                 (tenant_id, revision_id))
    if row is None:
        raise NotFound(f"goal revision '{revision_id}' not found")
    return row


def list_revisions(tx: Tx, tenant_id: str, goal_id: str) -> list[dict]:
    return tx.all("SELECT * FROM goal_revisions WHERE tenant_id = %s AND goal_id = %s ORDER BY created_at DESC",
                  (tenant_id, goal_id))


def decide_revision(ctx: Ctx, goal_id: str, revision_id: str, approve: bool, note: str,
                    expected_version: int | None) -> dict:
    goal = get(ctx.tx, ctx.tenant_id, goal_id, lock=True)
    _require_owner(ctx, goal, "goal.approve_revision" if approve else "goal.reject_revision")
    rev = get_revision(ctx.tx, ctx.tenant_id, revision_id, lock=True)
    if rev["goal_id"] != goal_id:
        raise NotFound(f"goal revision '{revision_id}' not found")
    if rev["status"] != "proposed":
        raise Conflict(f"this revision is already {rev['status']}", code="REVISION_ALREADY_DECIDED")
    check_version(goal, expected_version, "goal")
    if not approve:
        rev = ctx.tx.one(
            """UPDATE goal_revisions SET status = 'rejected', decided_by = %s, decided_at = now(), decision_note = %s,
                      updated_at = now(), version = version + 1 WHERE tenant_id = %s AND id = %s RETURNING *""",
            (ctx.actor_id, note, ctx.tenant_id, revision_id))
        emit(ctx, "cas.goal.revision_rejected.v1", "goal_revision", rev["id"], rev["version"], goal_id,
             {"goal_id": goal_id, "note": note})
        return {"goal": goal, "revision": rev}
    if rev["base_version"] != goal["version"]:
        raise Conflict("the goal changed after this revision was proposed; it must be proposed again against "
                       "the current version", code="REVISION_BASELINE_CHANGED",
                       details={"base_version": rev["base_version"], "current_version": goal["version"]})
    new = _apply(ctx, goal, rev["changes"], rev["hold_contract_ids"], rev["id"], rev["proposed_by"])
    ctx.tx.execute("UPDATE goal_revisions SET decision_note = %s WHERE tenant_id = %s AND id = %s",
                   (note, ctx.tenant_id, revision_id))
    return {"goal": new, "revision": get_revision(ctx.tx, ctx.tenant_id, revision_id)}
