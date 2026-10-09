"""Shared plumbing for commands: the request context, ids, events and the audit log."""
from __future__ import annotations

import hashlib
import json
import secrets
from dataclasses import dataclass, field, replace
from typing import Any

from cas.persistence.db import Tx

EVENT_SOURCE = "urn:cas:coordination"
RECONCILER_ID = "service_reconciler"


def new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(10)}"


def canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


def fingerprint(obj: Any) -> str:
    return hashlib.sha256(canonical(obj).encode()).hexdigest()


@dataclass
class Ctx:
    """Everything a command needs: the open transaction and who is acting, for which tenant."""
    tx: Tx
    tenant_id: str
    actor: dict
    request_id: str
    causation_id: str | None = None
    event_ids: list[str] = field(default_factory=list)

    @property
    def actor_id(self) -> str:
        return self.actor["id"]

    @property
    def is_human(self) -> bool:
        return self.actor["kind"] == "human"

    def caused_by(self, event_id: str) -> "Ctx":
        """Same transaction and event list, with a new causation id for follow-on events."""
        return replace(self, causation_id=event_id)


def emit(ctx: Ctx, type_: str, aggregate_type: str, aggregate_id: str, aggregate_version: int,
         goal_id: str | None, payload: dict | None = None) -> str:
    """Write a domain event and its outbox row in the caller's transaction (spec §9)."""
    event_id = new_id("evt")
    row = ctx.tx.one(
        """INSERT INTO events (tenant_id, id, source, type, subject, goal_id, aggregate_type, aggregate_id,
                               aggregate_version, actor_id, correlation_id, causation_id, payload)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING seq""",
        (ctx.tenant_id, event_id, EVENT_SOURCE, type_, f"{aggregate_type}s/{aggregate_id}", goal_id,
         aggregate_type, aggregate_id, aggregate_version, ctx.actor_id, ctx.request_id, ctx.causation_id,
         payload or {}),
    )
    ctx.tx.execute("INSERT INTO outbox (tenant_id, event_seq) VALUES (%s,%s)", (ctx.tenant_id, row["seq"]))
    ctx.event_ids.append(event_id)
    return event_id


def to_cloudevent(row: dict) -> dict:
    """Render a stored event as a CloudEvents 1.0 envelope (spec §2 Event)."""
    data = {
        "tenant_id": row["tenant_id"],
        "aggregate_id": row["aggregate_id"],
        "aggregate_type": row["aggregate_type"],
        "aggregate_version": row["aggregate_version"],
        "actor_id": row["actor_id"],
        "correlation_id": row["correlation_id"],
        "causation_id": row["causation_id"],
        "schema_version": row["schema_version"],
        "goal_id": row["goal_id"],
    }
    data.update(row["payload"] or {})
    return {
        "specversion": "1.0",
        "id": row["id"],
        "source": row["source"],
        "type": row["type"],
        "subject": row["subject"],
        "time": row["time"].isoformat(),
        "datacontenttype": "application/json",
        "data": data,
        "cursor": str(row["seq"]),
    }


def audit(tx: Tx, tenant_id: str, actor_id: str, action: str, resource_type: str, resource_id: str | None,
          decision: str, *, goal_id: str | None = None, reason: str | None = None,
          details: dict | None = None, request_id: str | None = None) -> str:
    audit_id = new_id("aud")
    tx.execute(
        """INSERT INTO audit_actions (tenant_id, id, actor_id, action, resource_type, resource_id, goal_id,
                                      decision, reason, details, request_id)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
        (tenant_id, audit_id, actor_id, action, resource_type, resource_id, goal_id, decision, reason,
         details or {}, request_id),
    )
    return audit_id


def bump(tx: Tx, table: str, tenant_id: str, id_: str, expected_version: int | None = None,
         **fields: Any) -> dict:
    """UPDATE one row, incrementing its version. With expected_version this is the optimistic
    concurrency check from spec §9; a mismatch raises VersionConflict."""
    from cas.domain.errors import NotFound, VersionConflict

    sets = ", ".join(f"{k} = %({k})s" for k in fields)
    sql = (f"UPDATE {table} SET {sets + ', ' if sets else ''}version = version + 1, updated_at = now() "
           f"WHERE tenant_id = %(_tenant)s AND id = %(_id)s")
    params = dict(fields, _tenant=tenant_id, _id=id_)
    if expected_version is not None:
        sql += " AND version = %(_expected)s"
        params["_expected"] = expected_version
    row = tx.one(sql + " RETURNING *", params)
    if row is None:
        current = tx.one(f"SELECT version FROM {table} WHERE tenant_id = %s AND id = %s", (tenant_id, id_))
        if current is None:
            raise NotFound(f"{table.rstrip('s')} '{id_}' not found")
        raise VersionConflict(
            f"{table.rstrip('s')} '{id_}' is at version {current['version']}, not {expected_version}",
            details={"current_version": current["version"], "expected_version": expected_version},
        )
    return row


def check_version(row: dict, expected: int | None, what: str) -> None:
    from cas.domain.errors import VersionConflict

    if expected is not None and row["version"] != expected:
        raise VersionConflict(
            f"{what} '{row['id']}' is at version {row['version']}, not {expected}",
            details={"current_version": row["version"], "expected_version": expected},
        )
