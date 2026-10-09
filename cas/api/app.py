"""CAS HTTP API (spec §5). REST and JSON; every mutating route goes through deps.command."""
from __future__ import annotations

import base64
import binascii
import logging
import os
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Body as JsonBody
from fastapi import FastAPI, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from cas.api import models as m
from cas.api.deps import command, error_response, if_match, query, request_id
from cas.domain.errors import CasError, NotFound, Unprocessable
from cas.persistence import blobstore, db
from cas.service import (artifacts, contracts, decisions, effects, goals, notifications, policy, principals, views)

log = logging.getLogger("cas.api")
UI_DIR = Path(__file__).resolve().parent.parent / "ui" / "static"
Limit = Query(default=50, ge=1, le=200)


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.migrate()
    stop = threading.Event()
    if os.environ.get("CAS_EMBEDDED_WORKER", "0") == "1":
        from cas.workers.runner import run_forever
        threading.Thread(target=run_forever, args=(stop,), daemon=True, name="cas-worker").start()
    yield
    stop.set()


app = FastAPI(title="Coordinated Agent Systems", version="0.1.0", lifespan=lifespan)


@app.exception_handler(CasError)
async def _cas_error(request: Request, err: CasError) -> JSONResponse:
    return error_response(request, err)


@app.exception_handler(RequestValidationError)
async def _validation_error(request: Request, err: RequestValidationError) -> JSONResponse:
    details = [{"path": "/".join(str(p) for p in e["loc"]), "message": e["msg"]} for e in err.errors()[:20]]
    return error_response(request, Unprocessable("the request body is not valid", code="SCHEMA_VIOLATION",
                                                 details={"errors": details}))


@app.exception_handler(Exception)
async def _unexpected(request: Request, err: Exception) -> JSONResponse:
    log.exception("unhandled error")
    return error_response(request, CasError("an unexpected error occurred"))


def dump(model: m.Body) -> dict:
    return model.model_dump(exclude_none=True)


# ------------------------------------------------------------------ identity and policy

@app.get("/healthz")
def healthz() -> dict:
    with db.transaction() as tx:
        return {"status": "ok", "outbox": notifications.outbox_health(tx)}


@app.get("/v1/me")
def me(request: Request):
    return query(request, lambda tx, a: dict(
        a, grants=principals.list_grants(tx, a["tenant_id"], a["id"])))


@app.post("/v1/me/attention")
def set_attention(request: Request, body: m.Attention):
    return command(request, body, "principal.set_attention",
                   lambda ctx: principals.set_attention_preference(ctx, body.level))


@app.get("/v1/principals")
def list_principals(request: Request):
    return query(request, lambda tx, a: {"items": principals.list_principals(tx, a["tenant_id"])})


@app.post("/v1/principals", status_code=201)
def create_principal(request: Request, body: m.PrincipalCreate):
    return command(request, body, "principal.create", lambda ctx: principals.create_principal(ctx, dump(body)),
                   status_code=201)


@app.post("/v1/agents", status_code=201)
def register_agent(request: Request, body: m.AgentCreate):
    return command(request, body, "agent.register", lambda ctx: principals.register_agent(ctx, dump(body)),
                   status_code=201)


@app.get("/v1/agents")
def list_agents(request: Request, capability: str | None = None, cursor: str | None = None, limit: int = Limit):
    return query(request, lambda tx, a: principals.list_agents(tx, a["tenant_id"], capability, cursor, limit))


@app.get("/v1/agents/{agent_id}")
def get_agent(request: Request, agent_id: str):
    return query(request, lambda tx, a: principals.get_agent(tx, a["tenant_id"], agent_id))


@app.get("/v1/grants")
def list_grants(request: Request, principal_id: str | None = None):
    return query(request, lambda tx, a: {"items": principals.list_grants(tx, a["tenant_id"], principal_id)})


@app.post("/v1/grants", status_code=201)
def create_grant(request: Request, body: m.GrantCreate):
    return command(request, body, "grant.create", lambda ctx: principals.create_grant(ctx, dump(body)),
                   status_code=201)


@app.post("/v1/grants/{grant_id}/revoke")
def revoke_grant(request: Request, grant_id: str):
    return command(request, {}, "grant.revoke", lambda ctx: principals.revoke_grant(ctx, grant_id))


@app.post("/v1/goals/{goal_id}/resource-policy")
def set_resource_policy(request: Request, goal_id: str, body: m.ResourcePolicy):
    return command(request, body, "resource_policy.set",
                   lambda ctx: principals.set_resource_policy(ctx, goal_id, body.allowed_scopes))


# ------------------------------------------------------------------ goals

@app.post("/v1/goals", status_code=201)
def create_goal(request: Request, body: dict[str, Any] = JsonBody(...)):
    return command(request, body, "goal.create", lambda ctx: goals.create(ctx, body), status_code=201)


@app.get("/v1/goals")
def list_goals(request: Request):
    return query(request, lambda tx, a: {"items": goals.list_goals(tx, a["tenant_id"])})


@app.get("/v1/goals/{goal_id}")
def get_goal(request: Request, goal_id: str):
    return query(request, lambda tx, a: goals.get(tx, a["tenant_id"], goal_id))


def _goal_status(status: str):
    def handler(request: Request, goal_id: str):
        return command(request, {}, f"goal.{status}",
                       lambda ctx: goals.set_status(ctx, goal_id, status, if_match(request)))
    return handler


app.post("/v1/goals/{goal_id}/activate")(_goal_status("active"))
app.post("/v1/goals/{goal_id}/pause")(_goal_status("paused"))
app.post("/v1/goals/{goal_id}/complete")(_goal_status("completed"))
app.post("/v1/goals/{goal_id}/cancel")(_goal_status("cancelled"))


@app.post("/v1/goals/{goal_id}/revisions", status_code=201)
def revise_goal(request: Request, goal_id: str, body: m.GoalRevision):
    def run(ctx):
        out = goals.revise(ctx, goal_id, dump(body), if_match(request, required=False))
        return dict(out["goal"], revision=out["revision"])
    return command(request, body, "goal.revise", run, status_code=201)


@app.get("/v1/goals/{goal_id}/revisions")
def list_goal_revisions(request: Request, goal_id: str):
    return query(request, lambda tx, a: {"items": goals.list_revisions(tx, a["tenant_id"], goals.get(tx, a["tenant_id"], goal_id)["id"])})


def _decide_revision(approve: bool):
    def handler(request: Request, goal_id: str, revision_id: str, body: m.RevisionDecision):
        def run(ctx):
            out = goals.decide_revision(ctx, goal_id, revision_id, approve, body.note, if_match(request))
            return dict(out["goal"], revision=out["revision"])
        return command(request, body, "goal.approve_revision" if approve else "goal.reject_revision", run)
    return handler


app.post("/v1/goals/{goal_id}/revisions/{revision_id}/approve")(_decide_revision(True))
app.post("/v1/goals/{goal_id}/revisions/{revision_id}/reject")(_decide_revision(False))


@app.post("/v1/goals/{goal_id}/artifacts", status_code=201)
def publish_goal_input(request: Request, goal_id: str, body: m.ArtifactContent):
    return command(request, body, "artifact.publish_input",
                   lambda ctx: dict(artifacts.register_goal_input(ctx, goal_id, dump(body)), goal_id=goal_id),
                   status_code=201, resource_type="artifact")


@app.get("/v1/goals/{goal_id}/events")
def goal_events(request: Request, goal_id: str, cursor: str | None = None, subject: str | None = None,
                limit: int = Limit):
    return query(request, lambda tx, a: views.goal_events(tx, a["tenant_id"], goal_id, cursor, limit, subject))


@app.get("/v1/goals/{goal_id}/overview")
def goal_overview(request: Request, goal_id: str):
    return query(request, lambda tx, a: views.overview(tx, a["tenant_id"], goal_id), etag_from="none")


@app.get("/v1/goals/{goal_id}/exceptions")
def goal_exceptions(request: Request, goal_id: str):
    return query(request, lambda tx, a: views.exceptions(tx, a["tenant_id"], goal_id))


@app.get("/v1/events/{event_id}")
def get_event(request: Request, event_id: str):
    return query(request, lambda tx, a: views.get_event(tx, a["tenant_id"], event_id))


# ------------------------------------------------------------------ contracts

@app.post("/v1/contracts", status_code=201)
def create_contract(request: Request, body: dict[str, Any] = JsonBody(...)):
    return command(request, body, "contract.create", lambda ctx: contracts.view(ctx.tx, contracts.create(ctx, body)),
                   status_code=201)


@app.get("/v1/contracts")
def list_contracts(request: Request, goal_id: str | None = None, status: str | None = None,
                   cursor: str | None = None, limit: int = Limit):
    return query(request, lambda tx, a: contracts.list_contracts(tx, a["tenant_id"], goal_id, status, cursor, limit))


@app.get("/v1/contracts/{contract_id}")
def get_contract(request: Request, contract_id: str):
    return query(request, lambda tx, a: views.contract_detail(tx, a["tenant_id"], contract_id))


def _view(ctx, c: dict, lease_token: str | None = None) -> dict:
    return contracts.view(ctx.tx, c, lease_token)


@app.post("/v1/contracts/{contract_id}/dependencies", status_code=201)
def add_dependency(request: Request, contract_id: str, body: m.Dependency):
    return command(request, body, "contract.add_dependency",
                   lambda ctx: _view(ctx, contracts.add_dependency(ctx, contract_id, body.upstream_id, if_match(request))),
                   status_code=201)


@app.post("/v1/contracts/{contract_id}/revise")
def revise_contract(request: Request, contract_id: str, body: m.ContractRevision):
    return command(request, body, "contract.revise",
                   lambda ctx: _view(ctx, contracts.revise(ctx, contract_id, body.model_dump(), if_match(request))))


@app.post("/v1/contracts/{contract_id}/claim")
def claim_contract(request: Request, contract_id: str, body: m.Claim):
    def run(ctx):
        c, lease_token = contracts.claim(ctx, contract_id, body.worker_id, body.lease_seconds,
                                         if_match(request, required=False))
        return _view(ctx, c, lease_token)
    return command(request, body, "contract.claim", run)


@app.post("/v1/contracts/{contract_id}/heartbeat")
def heartbeat(request: Request, contract_id: str, body: m.Heartbeat):
    return command(request, body, "contract.heartbeat",
                   lambda ctx: _view(ctx, contracts.heartbeat(ctx, contract_id, body.lease_token, body.lease_seconds),
                                     body.lease_token))


@app.post("/v1/contracts/{contract_id}/start")
def start_contract(request: Request, contract_id: str, body: m.Lease):
    return command(request, body, "contract.start",
                   lambda ctx: _view(ctx, contracts.start(ctx, contract_id, body.lease_token), body.lease_token))


@app.post("/v1/contracts/{contract_id}/block")
def block_contract(request: Request, contract_id: str, body: m.Block):
    return command(request, body, "contract.block",
                   lambda ctx: _view(ctx, contracts.block(ctx, contract_id, body.reason, body.lease_token,
                                                          if_match(request, required=body.lease_token is None))))


@app.post("/v1/contracts/{contract_id}/revalidate")
def revalidate_contract(request: Request, contract_id: str):
    return command(request, {}, "contract.revalidate",
                   lambda ctx: _view(ctx, contracts.revalidate(ctx, contract_id, if_match(request))))


@app.post("/v1/contracts/{contract_id}/fail")
def fail_contract(request: Request, contract_id: str, body: m.Fail):
    return command(request, body, "contract.fail",
                   lambda ctx: _view(ctx, contracts.fail(ctx, contract_id, body.lease_token, body.reason, body.retryable)))


@app.post("/v1/contracts/{contract_id}/retry")
def retry_contract(request: Request, contract_id: str):
    return command(request, {}, "contract.retry",
                   lambda ctx: _view(ctx, contracts.retry(ctx, contract_id, if_match(request))))


@app.post("/v1/contracts/{contract_id}/cancel")
def cancel_contract(request: Request, contract_id: str, body: m.Note):
    return command(request, body, "contract.cancel",
                   lambda ctx: _view(ctx, contracts.cancel(ctx, contract_id, body.reason, if_match(request))))


@app.post("/v1/contracts/{contract_id}/artifacts", status_code=201)
def register_artifact(request: Request, contract_id: str, body: m.ArtifactRegister):
    def run(ctx):
        c, version = contracts.register_artifact(ctx, contract_id, body.lease_token,
                                                 {k: v for k, v in dump(body).items() if k != "lease_token"})
        return dict(version, goal_id=c["goal_id"])
    return command(request, body, "artifact.register", run, status_code=201, resource_type="artifact")


@app.post("/v1/contracts/{contract_id}/submit")
def submit_contract(request: Request, contract_id: str, body: m.Lease):
    return command(request, body, "contract.submit",
                   lambda ctx: _view(ctx, contracts.submit(ctx, contract_id, body.lease_token)))


@app.post("/v1/contracts/{contract_id}/accept")
def accept_contract(request: Request, contract_id: str, body: m.Accept):
    return command(request, body, "contract.accept",
                   lambda ctx: _view(ctx, contracts.accept(ctx, contract_id, [dump(r) for r in body.check_results],
                                                           if_match(request))))


@app.post("/v1/contracts/{contract_id}/reject")
def reject_contract(request: Request, contract_id: str, body: m.Reject):
    return command(request, body, "contract.reject",
                   lambda ctx: _view(ctx, contracts.reject(ctx, contract_id, body.reason,
                                                           [dump(r) for r in body.check_results], if_match(request))))


# ------------------------------------------------------------------ side effects

@app.post("/v1/contracts/{contract_id}/effects", status_code=201)
def authorize_effect(request: Request, contract_id: str, body: m.EffectRequest):
    return command(request, body, "effect.authorize",
                   lambda ctx: effects.authorize(ctx, contract_id, body.lease_token, dump(body)),
                   status_code=201, resource_type="effect")


@app.post("/v1/contracts/{contract_id}/effects/{effect_id}/result")
def report_effect(request: Request, contract_id: str, effect_id: str, body: m.EffectResult):
    return command(request, body, "effect.report",
                   lambda ctx: effects.report(ctx, contract_id, effect_id, body.lease_token,
                                              body.status == "succeeded", body.detail), resource_type="effect")


@app.post("/v1/effects/{effect_id}/recover")
def recover_effect(request: Request, effect_id: str, body: m.EffectRecovery):
    return command(request, body, "effect.recover",
                   lambda ctx: effects.recover(ctx, effect_id, body.outcome, body.note), resource_type="effect")


# ------------------------------------------------------------------ artifacts and blobs

@app.get("/v1/artifacts/{artifact_id}")
def get_artifact(request: Request, artifact_id: str):
    return query(request, lambda tx, a: artifacts.get_artifact(tx, a["tenant_id"], artifact_id))


@app.get("/v1/artifacts/{artifact_id}/versions/{version_id}/content")
def get_artifact_content(request: Request, artifact_id: str, version_id: str):
    from cas.api.deps import authenticate
    with db.transaction() as tx:
        actor = authenticate(tx, request)
        row, data = artifacts.read_content(tx, actor["tenant_id"], artifact_id, version_id)
    # Stored content is untrusted: never let a browser render or sniff it as a page.
    return Response(content=data, media_type="application/octet-stream",
                    headers={"ETag": f'"{row["sha256"]}"', "X-Content-Type-Options": "nosniff",
                             "Content-Disposition": "attachment", "X-CAS-Media-Type": row["media_type"][:100]})


@app.post("/v1/blobs", status_code=201)
def upload_blob(request: Request, body: m.Blob):
    def run(ctx):
        try:
            data = base64.b64decode(body.content_base64, validate=True)
        except (binascii.Error, ValueError):
            raise Unprocessable("content_base64 is not valid base64", code="INVALID_CONTENT") from None
        from cas.config import settings
        if len(data) > settings.max_blob_bytes:
            raise Unprocessable("blob exceeds the size limit", code="ARTIFACT_TOO_LARGE")
        uri, sha = blobstore.put(ctx.tenant_id, data)
        return {"uri": uri, "sha256": sha, "size_bytes": len(data)}
    return command(request, body, "blob.upload", run, status_code=201, resource_type="blob")


# ------------------------------------------------------------------ decisions and conflicts

@app.post("/v1/decisions", status_code=201)
def create_decision(request: Request, body: m.DecisionCreate):
    return command(request, body, "decision.create", lambda ctx: decisions.create(ctx, dump(body)), status_code=201)


@app.get("/v1/decisions/{decision_id}")
def get_decision(request: Request, decision_id: str):
    return query(request, lambda tx, a: decisions.view(tx, decisions.get(tx, a["tenant_id"], decision_id)))


@app.post("/v1/decisions/{decision_id}/resolve")
def resolve_decision(request: Request, decision_id: str, body: m.DecisionResolve):
    return command(request, body, "decision.resolve",
                   lambda ctx: decisions.resolve(ctx, decision_id, dump(body), if_match(request)))


@app.post("/v1/decisions/{decision_id}/refresh")
def refresh_decision(request: Request, decision_id: str, body: m.DecisionRefresh):
    return command(request, body, "decision.refresh",
                   lambda ctx: decisions.refresh(ctx, decision_id, dump(body), if_match(request)))


@app.post("/v1/decisions/{decision_id}/cancel")
def cancel_decision(request: Request, decision_id: str, body: m.Note):
    return command(request, body, "decision.cancel",
                   lambda ctx: decisions.cancel(ctx, decision_id, body.reason, if_match(request)))


@app.post("/v1/conflicts", status_code=201)
def open_conflict(request: Request, body: m.ConflictCreate):
    return command(request, body, "conflict.create", lambda ctx: decisions.open_conflict(ctx, dump(body)),
                   status_code=201)


@app.get("/v1/conflicts/{conflict_id}")
def get_conflict(request: Request, conflict_id: str):
    return query(request, lambda tx, a: decisions.get_conflict(tx, a["tenant_id"], conflict_id))


@app.post("/v1/conflicts/{conflict_id}/rounds")
def conflict_round(request: Request, conflict_id: str, body: m.ConflictRound):
    return command(request, body, "conflict.round",
                   lambda ctx: decisions.add_round(ctx, conflict_id, dump(body), if_match(request)))


@app.post("/v1/conflicts/{conflict_id}/escalate")
def escalate_conflict(request: Request, conflict_id: str, body: m.ConflictEscalate):
    return command(request, body, "conflict.escalate",
                   lambda ctx: decisions.escalate_command(ctx, conflict_id, {k: v for k, v in dump(body).items() if v},
                                                          if_match(request)))


# ------------------------------------------------------------------ notifications, audit, operations

@app.get("/v1/notifications")
def list_notifications(request: Request, unread: bool = False, limit: int = Limit):
    return query(request, lambda tx, a: {"items": tx.all(
        """SELECT * FROM notifications WHERE tenant_id = %s AND recipient_id = %s AND (NOT %s OR read_at IS NULL)
           ORDER BY created_at DESC LIMIT %s""", (a["tenant_id"], a["id"], unread, limit))})


@app.get("/v1/notifications/digest")
def notification_digest(request: Request):
    """A summary of unread digest-level notifications. It reports open exceptions from live
    state and never marks anything resolved (spec §8)."""
    def run(tx, a):
        rows = tx.all(
            """SELECT goal_id, event_type, count(*) AS count, max(created_at) AS latest FROM notifications
               WHERE tenant_id = %s AND recipient_id = %s AND read_at IS NULL AND level IN ('ambient','digest')
               GROUP BY goal_id, event_type ORDER BY goal_id, event_type""", (a["tenant_id"], a["id"]))
        goal_ids = sorted({r["goal_id"] for r in rows if r["goal_id"]})
        return {"groups": rows, "open_exceptions": {g: len(views.exceptions(tx, a["tenant_id"], g)["items"]) for g in goal_ids}}
    return query(request, run)


@app.post("/v1/notifications/read")
def mark_read(request: Request, body: m.MarkRead):
    def run(ctx):
        n = ctx.tx.execute(
            "UPDATE notifications SET read_at = now() WHERE tenant_id = %s AND recipient_id = %s AND id = ANY(%s::text[]) AND read_at IS NULL",
            (ctx.tenant_id, ctx.actor_id, "{" + ",".join(i.replace(",", "") for i in body.ids) + "}")).rowcount
        return {"marked_read": n}
    return command(request, body, "notification.read", run)


@app.get("/v1/audit")
def list_audit(request: Request, goal_id: str | None = None, decision: str | None = None, limit: int = Limit):
    def run(tx, a):
        roles = policy.roles(tx, a["tenant_id"], a["id"])
        if a["kind"] != "human" or not roles & {"auditor", "admin"}:
            raise policy.PolicyDenied("reading the audit log requires the 'auditor' or 'admin' role",
                                      action="audit.read", resource_type="audit")
        return {"items": tx.all(
            """SELECT * FROM audit_actions WHERE tenant_id = %s AND (%s::text IS NULL OR goal_id = %s)
                 AND (%s::text IS NULL OR decision = %s) ORDER BY created_at DESC LIMIT %s""",
            (a["tenant_id"], goal_id, goal_id, decision, decision, limit))}
    return query(request, run)


@app.post("/v1/admin/tick")
def admin_tick(request: Request):
    """Run one pass of the reconciler, validation and outbox workers for this tenant now."""
    def run(ctx):
        policy.require_admin(ctx, "admin.tick")
        return {"scheduled": True}
    response = command(request, {}, "admin.tick", run)
    from cas.api.deps import authenticate
    from cas.workers.runner import tick
    with db.transaction() as tx:
        tenant_id = authenticate(tx, request)["tenant_id"]
    tick(None, tenant_id)
    return response


# ------------------------------------------------------------------ workspace UI

@app.get("/", include_in_schema=False)
def ui_index():
    index = UI_DIR / "index.html"
    if not index.exists():
        raise NotFound("the workspace UI is not installed")
    return FileResponse(index, headers={"Cache-Control": "no-store"})


class _UiFiles(StaticFiles):
    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-cache"  # always revalidate, so a deploy is picked up at once
        return response


if UI_DIR.exists():
    app.mount("/static", _UiFiles(directory=UI_DIR), name="static")
