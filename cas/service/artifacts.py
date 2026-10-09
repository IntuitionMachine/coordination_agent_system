"""Artifacts and their immutable versions (spec §2 ArtifactVersion, §9)."""
from __future__ import annotations

import base64
import binascii
import json
import re

from cas.config import settings
from cas.domain.errors import Conflict, NotFound, Unprocessable
from cas.persistence import blobstore
from cas.persistence.db import Tx
from cas.service import policy
from cas.service.core import Ctx, bump, emit, new_id

_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")


def get_artifact(tx: Tx, tenant_id: str, artifact_id: str) -> dict:
    row = tx.one("SELECT * FROM artifacts WHERE tenant_id = %s AND id = %s", (tenant_id, artifact_id))
    if row is None:
        raise NotFound(f"artifact '{artifact_id}' not found")
    out = dict(row)
    out["versions"] = tx.all(
        "SELECT * FROM artifact_versions WHERE tenant_id = %s AND artifact_id = %s ORDER BY version_no",
        (tenant_id, artifact_id))
    return out


def versions_for_contract(tx: Tx, tenant_id: str, contract_id: str) -> list[dict]:
    return tx.all(
        """SELECT v.*, a.artifact_type FROM artifact_versions v
             JOIN artifacts a ON a.tenant_id = v.tenant_id AND a.id = v.artifact_id
           WHERE v.tenant_id = %s AND v.contract_id = %s ORDER BY v.created_at, v.version_no""",
        (tenant_id, contract_id))


def _ingest(tenant_id: str, body: dict) -> tuple[str, str, int]:
    """Turn the request's content into an immutable blob and verify any claimed hash (spec §9)."""
    given = [k for k in ("content_text", "content_json", "content_base64", "uri") if body.get(k) is not None]
    if len(given) != 1:
        raise Unprocessable("provide exactly one of content_text, content_json, content_base64 or uri",
                            code="ARTIFACT_CONTENT_REQUIRED")
    claimed = body.get("sha256")
    if given[0] == "uri":
        sha = blobstore.sha_from_uri(body["uri"])
        if sha is None or not blobstore.exists(tenant_id, sha):
            raise Unprocessable("uri must be a cas://sha256/<hash> blob already uploaded by this tenant",
                                code="UNKNOWN_BLOB")
        data = blobstore.get(tenant_id, sha)
    else:
        if given[0] == "content_text":
            data = body["content_text"].encode()
        elif given[0] == "content_json":
            data = json.dumps(body["content_json"], sort_keys=True, separators=(",", ":")).encode()
        else:
            try:
                data = base64.b64decode(body["content_base64"], validate=True)
            except (binascii.Error, ValueError):
                raise Unprocessable("content_base64 is not valid base64", code="INVALID_CONTENT") from None
        if len(data) > settings.max_blob_bytes:
            raise Unprocessable("artifact content exceeds the size limit", code="ARTIFACT_TOO_LARGE")
        _, sha = blobstore.put(tenant_id, data)
    if claimed and claimed.lower() != sha:
        raise Unprocessable("sha256 does not match the artifact content", code="HASH_MISMATCH",
                            details={"claimed": claimed, "actual": sha})
    return blobstore.uri_for(sha), sha, len(data)


def _supersession_chain(tx: Tx, tenant_id: str, contract_id: str) -> set[str]:
    rows = tx.all(
        """WITH RECURSIVE chain AS (
             SELECT id, supersedes_contract_id FROM contracts WHERE tenant_id = %(t)s AND id = %(c)s
             UNION
             SELECT c.id, c.supersedes_contract_id FROM contracts c
               JOIN chain ON c.id = chain.supersedes_contract_id WHERE c.tenant_id = %(t)s)
           SELECT id FROM chain""",
        {"t": tenant_id, "c": contract_id})
    return {r["id"] for r in rows}


def _next_version(ctx: Ctx, artifact_id: str, goal_id: str, artifact_type: str,
                  origin_contract_id: str | None, allowed_origins: set[str] | None) -> tuple[dict, int]:
    """Lock (or create) the artifact and reserve its next version number."""
    tx = ctx.tx
    if not _ID.match(artifact_id):
        raise Unprocessable("artifact_id may contain only letters, digits and _ . : -", code="INVALID_ARTIFACT_ID")
    tx.execute(
        """INSERT INTO artifacts (tenant_id, id, goal_id, artifact_type, origin_contract_id)
           VALUES (%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING""",
        (ctx.tenant_id, artifact_id, goal_id, artifact_type, origin_contract_id))
    art = tx.one("SELECT * FROM artifacts WHERE tenant_id = %s AND id = %s FOR UPDATE", (ctx.tenant_id, artifact_id))
    if art["goal_id"] != goal_id:
        raise Unprocessable(f"artifact '{artifact_id}' belongs to a different goal", code="ARTIFACT_GOAL_MISMATCH")
    if art["artifact_type"] != artifact_type:
        raise Unprocessable(f"artifact '{artifact_id}' has type '{art['artifact_type']}', not '{artifact_type}'",
                            code="ARTIFACT_TYPE_MISMATCH")
    if allowed_origins is not None and art["origin_contract_id"] not in allowed_origins:
        raise policy.deny(
            ctx, f"artifact '{artifact_id}' is produced by other work; only its originating contract or a "
                 "successor may publish a new version",
            action="artifact.register", resource_type="artifact", resource_id=artifact_id, goal_id=goal_id)
    if allowed_origins is None and art["origin_contract_id"] is not None:
        raise Unprocessable(f"artifact '{artifact_id}' is produced by a contract and cannot be replaced as a goal input",
                            code="ARTIFACT_NOT_GOAL_INPUT")
    art = bump(tx, "artifacts", ctx.tenant_id, artifact_id, version_counter=art["version_counter"] + 1)
    return art, art["version_counter"]


def register_for_contract(ctx: Ctx, contract: dict, body: dict) -> dict:
    """Register a proposed artifact version produced by the contract's current attempt.
    The caller has already verified the lease. Proposed versions are never auto-accepted."""
    artifact_type = body["type"]
    if artifact_type not in {d["type"] for d in contract["deliverable_spec"]}:
        raise Unprocessable(f"type '{artifact_type}' is not in the contract's deliverable_spec",
                            code="UNEXPECTED_DELIVERABLE")
    artifact_id = body.get("artifact_id") or new_id("art")
    uri, sha, size = _ingest(ctx.tenant_id, body)
    art, n = _next_version(ctx, artifact_id, contract["goal_id"], artifact_type, contract["id"],
                           _supersession_chain(ctx.tx, ctx.tenant_id, contract["id"]))
    row = ctx.tx.one(
        """INSERT INTO artifact_versions (tenant_id, artifact_id, version_id, version_no, contract_id,
               fencing_token, attempt_no, producer_id, uri, sha256, media_type, schema_ref, size_bytes,
               input_lineage, status)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'proposed') RETURNING *""",
        (ctx.tenant_id, artifact_id, f"v{n}", n, contract["id"], contract["fencing_token"], contract["attempt"],
         ctx.actor_id, uri, sha, body.get("media_type") or "application/octet-stream", body.get("schema_ref"),
         size, contract["pinned_inputs"]))
    emit(ctx, "cas.artifact.proposed.v1", "artifact", artifact_id, art["version"], contract["goal_id"],
         {"version_id": row["version_id"], "contract_id": contract["id"], "sha256": sha,
          "attempt": contract["attempt"], "input_lineage": contract["pinned_inputs"]})
    return dict(row, artifact_type=artifact_type)


def register_goal_input(ctx: Ctx, goal_id: str, body: dict) -> dict:
    """A human with authority over the goal publishes an input artifact version. It is accepted
    immediately, supersedes the previous version, and invalidates work pinned to that version."""
    from cas.service import contracts

    goal = ctx.tx.one("SELECT * FROM goals WHERE tenant_id = %s AND id = %s", (ctx.tenant_id, goal_id))
    if goal is None:
        raise NotFound(f"goal '{goal_id}' not found")
    allowed = ctx.is_human and (goal["owner_principal_id"] == ctx.actor_id or policy.is_admin(ctx)
                                or policy.has_role(ctx.tx, ctx.tenant_id, ctx.actor_id, "task_requester", goal_id))
    if not allowed:
        raise policy.deny(ctx, "only a human goal owner, task requester or admin may publish goal inputs",
                          action="artifact.publish_input", resource_type="goal", resource_id=goal_id, goal_id=goal_id)
    artifact_id = body.get("artifact_id") or new_id("art")
    uri, sha, size = _ingest(ctx.tenant_id, body)
    art, n = _next_version(ctx, artifact_id, goal_id, body["type"], None, None)
    old = art["current_version_id"]
    row = ctx.tx.one(
        """INSERT INTO artifact_versions (tenant_id, artifact_id, version_id, version_no, producer_id, uri,
               sha256, media_type, schema_ref, size_bytes, status)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'accepted') RETURNING *""",
        (ctx.tenant_id, artifact_id, f"v{n}", n, ctx.actor_id, uri, sha,
         body.get("media_type") or "application/octet-stream", body.get("schema_ref"), size))
    art = _make_current(ctx, art, row["version_id"])
    event_id = emit(ctx, "cas.artifact.accepted.v1", "artifact", artifact_id, art["version"], goal_id,
                    {"version_id": row["version_id"], "supersedes": old, "sha256": sha, "goal_input": True})
    cause = ctx.caused_by(event_id)
    if old:
        contracts.propagate_invalidation(cause, artifact_id, old, row["version_id"])
    contracts.reconcile_goal(cause, goal_id)
    return dict(row, artifact_type=body["type"])


def _make_current(ctx: Ctx, art: dict, version_id: str) -> dict:
    """Point the artifact at a newly accepted version and mark the previous one superseded."""
    old = art["current_version_id"]
    if old and old != version_id:
        ctx.tx.execute(
            """UPDATE artifact_versions SET status = 'superseded', updated_at = now(), version = version + 1
               WHERE tenant_id = %s AND artifact_id = %s AND version_id = %s AND status = 'accepted'""",
            (ctx.tenant_id, art["id"], old))
    return bump(ctx.tx, "artifacts", ctx.tenant_id, art["id"], current_version_id=version_id)


def accept_attempt_versions(ctx: Ctx, contract: dict) -> list[tuple[str, str | None, str, str]]:
    """Accept the proposed versions of the contract's current attempt. Returns, per artifact,
    (artifact_id, superseded_version_id, accepted_version_id, event_id)."""
    rows = ctx.tx.all(
        """SELECT * FROM artifact_versions WHERE tenant_id = %s AND contract_id = %s AND fencing_token = %s
             AND status = 'proposed' ORDER BY artifact_id, version_no""",
        (ctx.tenant_id, contract["id"], contract["fencing_token"]))
    latest: dict[str, dict] = {}
    for r in rows:
        latest[r["artifact_id"]] = r  # the last version an attempt proposed for an artifact wins
    changes = []
    for r in rows:
        winner = latest[r["artifact_id"]]["version_id"] == r["version_id"]
        ctx.tx.execute(
            """UPDATE artifact_versions SET status = %s, updated_at = now(), version = version + 1
               WHERE tenant_id = %s AND artifact_id = %s AND version_id = %s""",
            ("accepted" if winner else "superseded", ctx.tenant_id, r["artifact_id"], r["version_id"]))
        if not winner:
            continue
        art = ctx.tx.one("SELECT * FROM artifacts WHERE tenant_id = %s AND id = %s FOR UPDATE",
                         (ctx.tenant_id, r["artifact_id"]))
        old = art["current_version_id"]
        art = _make_current(ctx, art, r["version_id"])
        event_id = emit(ctx, "cas.artifact.accepted.v1", "artifact", art["id"], art["version"], contract["goal_id"],
                        {"version_id": r["version_id"], "supersedes": old, "sha256": r["sha256"],
                         "contract_id": contract["id"]})
        changes.append((art["id"], old, r["version_id"], event_id))
    return changes


def reject_attempt_versions(ctx: Ctx, contract: dict, reason: str) -> list[str]:
    """Mark the current attempt's proposed versions rejected. They are kept, never deleted (T11)."""
    rows = ctx.tx.all(
        """UPDATE artifact_versions SET status = 'rejected', updated_at = now(), version = version + 1
           WHERE tenant_id = %s AND contract_id = %s AND fencing_token = %s AND status = 'proposed'
           RETURNING artifact_id, version_id""",
        (ctx.tenant_id, contract["id"], contract["fencing_token"]))
    for r in rows:
        art = bump(ctx.tx, "artifacts", ctx.tenant_id, r["artifact_id"])
        emit(ctx, "cas.artifact.rejected.v1", "artifact", art["id"], art["version"], contract["goal_id"],
             {"version_id": r["version_id"], "contract_id": contract["id"], "reason": reason})
    return [f"{r['artifact_id']}@{r['version_id']}" for r in rows]


def read_content(tx: Tx, tenant_id: str, artifact_id: str, version_id: str) -> tuple[dict, bytes]:
    row = tx.one("SELECT * FROM artifact_versions WHERE tenant_id = %s AND artifact_id = %s AND version_id = %s",
                 (tenant_id, artifact_id, version_id))
    if row is None:
        raise NotFound(f"artifact version '{artifact_id}@{version_id}' not found")
    data = blobstore.get(tenant_id, row["sha256"])
    if data is None:
        raise Conflict("artifact content is missing from storage", code="BLOB_MISSING")
    return row, data
