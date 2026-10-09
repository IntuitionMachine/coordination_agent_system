"""Request plumbing shared by every route: authentication, If-Match, idempotency and the
transaction + audit wrapper around commands (spec §5)."""
from __future__ import annotations

import logging
import time
from typing import Any, Callable

import psycopg
from fastapi import Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse

from cas.domain.errors import (CasError, Conflict, PolicyDenied, PreconditionRequired, Unauthenticated,
                               Unavailable, Unprocessable)
from cas.persistence.db import Tx, transaction
from cas.service import decisions, principals
from cas.service.core import Ctx, audit, fingerprint, new_id

log = logging.getLogger("cas.api")
RETRYABLE = (psycopg.errors.DeadlockDetected, psycopg.errors.SerializationFailure)


def request_id(request: Request) -> str:
    if not hasattr(request.state, "request_id"):
        request.state.request_id = request.headers.get("x-request-id") or new_id("req")
    return request.state.request_id


def authenticate(tx: Tx, request: Request) -> dict:
    """Resolve the bearer credential to a principal. The tenant always comes from the
    credential, never from the request, so one tenant cannot address another's data (T17)."""
    header = request.headers.get("authorization", "")
    scheme, _, credential = header.partition(" ")
    if scheme.lower() != "bearer" or not credential.strip():
        raise Unauthenticated("a bearer credential is required")
    actor = principals.authenticate(tx, credential.strip())
    if actor is None:
        raise Unauthenticated("the credential is not valid")
    return actor


def if_match(request: Request, required: bool = True) -> int | None:
    """Parse If-Match as the object's version (spec §5). Missing where required is a 428."""
    raw = request.headers.get("if-match")
    if raw is None:
        if required:
            raise PreconditionRequired("If-Match with the object's current version is required")
        return None
    value = raw.strip().removeprefix("W/").strip('"')
    if not value.isdigit():
        raise Unprocessable("If-Match must be the object's version number", code="INVALID_IF_MATCH")
    return int(value)


def error_response(request: Request, err: CasError) -> JSONResponse:
    return JSONResponse(status_code=err.status, content=jsonable_encoder(
        {"error": {"code": err.code, "message": err.message, "details": err.details},
         "request_id": request_id(request)}))


def _record_denial(request: Request, actor: dict, err: PolicyDenied) -> None:
    try:
        with transaction() as tx:
            decisions.record_denial(Ctx(tx=tx, tenant_id=actor["tenant_id"], actor=actor,
                                        request_id=request_id(request)), err)
    except Exception:  # noqa: BLE001 - the 403 must still be returned
        log.exception("could not record policy denial")


def query(request: Request, fn: Callable[[Tx, dict], Any], etag_from: str | None = None) -> JSONResponse:
    """Run a read. fn(tx, actor) returns the JSON body."""
    with transaction() as tx:
        actor = authenticate(tx, request)
        body = fn(tx, actor)
    headers = {}
    if etag_from is None and isinstance(body, dict) and "version" in body:
        headers["ETag"] = f'"{body["version"]}"'
    return JSONResponse(content=jsonable_encoder(body), headers=headers)


def command(request: Request, body: Any, action: str, fn: Callable[[Ctx], dict], *, status_code: int = 200,
            resource_type: str | None = None) -> JSONResponse:
    """Run a mutating command in one transaction together with its idempotency record, its
    events and its audit row. The same Idempotency-Key replays the stored response; the same key
    with a different request is a 409. A policy denial rolls the command back and is then
    audited (and escalated) in a transaction of its own."""
    rid = request_id(request)
    key = request.headers.get("idempotency-key")
    if not key or len(key) > 200:
        raise Unprocessable("mutating requests must carry an Idempotency-Key header (1-200 characters)",
                            code="IDEMPOTENCY_KEY_REQUIRED")
    print_ = fingerprint({"method": request.method, "path": request.url.path, "body": jsonable_encoder(body),
                          "if_match": request.headers.get("if-match")})
    actor: dict | None = None
    for attempt in range(4):
        try:
            with transaction() as tx:
                actor = authenticate(tx, request)
                claimed = tx.one(
                    """INSERT INTO idempotency_records (tenant_id, actor_id, key, fingerprint) VALUES (%s,%s,%s,%s)
                       ON CONFLICT DO NOTHING RETURNING key""",
                    (actor["tenant_id"], actor["id"], key, print_))
                if claimed is None:
                    prior = tx.one("SELECT * FROM idempotency_records WHERE tenant_id = %s AND actor_id = %s AND key = %s",
                                   (actor["tenant_id"], actor["id"], key))
                    if prior["fingerprint"] != print_:
                        raise Conflict("this Idempotency-Key was already used for a different request",
                                       code="IDEMPOTENCY_KEY_REUSED")
                    return JSONResponse(status_code=prior["status_code"], content=prior["response"],
                                        headers={**(prior["headers"] or {}), "Idempotent-Replay": "true"})
                ctx = Ctx(tx=tx, tenant_id=actor["tenant_id"], actor=actor, request_id=rid)
                resource = fn(ctx)
                payload = jsonable_encoder({"resource": resource, "event_ids": ctx.event_ids, "request_id": rid})
                headers = {}
                if isinstance(resource, dict) and "version" in resource:
                    headers["ETag"] = f'"{resource["version"]}"'
                audit(tx, actor["tenant_id"], actor["id"], action, resource_type or action.split(".")[0],
                      resource.get("id") if isinstance(resource, dict) else None, "allow",
                      goal_id=resource.get("goal_id") if isinstance(resource, dict) else None,
                      details={"event_ids": ctx.event_ids}, request_id=rid)
                tx.execute(
                    "UPDATE idempotency_records SET status_code = %s, response = %s, headers = %s WHERE tenant_id = %s AND actor_id = %s AND key = %s",
                    (status_code, payload, headers, actor["tenant_id"], actor["id"], key))
            return JSONResponse(status_code=status_code, content=payload, headers=headers)
        except RETRYABLE:
            if attempt == 3:
                raise Unavailable("the database could not complete this request; retry it") from None
            time.sleep(0.02 * (attempt + 1))
        except PolicyDenied as err:
            if actor is not None:
                _record_denial(request, actor, err)
            raise
    raise Unavailable("the database could not complete this request; retry it")
