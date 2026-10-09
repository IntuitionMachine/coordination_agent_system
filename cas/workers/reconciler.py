"""The reconciler (spec §10): expires leases, re-evaluates readiness, and applies bounded
automatic retries. Each contract is handled in its own transaction."""
from __future__ import annotations

import logging

from cas.persistence.db import Tx, transaction
from cas.service import contracts
from cas.service.core import RECONCILER_ID, Ctx, new_id

log = logging.getLogger("cas.reconciler")


def system_ctx(tx: Tx, tenant_id: str, request_id: str | None = None) -> Ctx:
    actor = tx.one("SELECT * FROM principals WHERE tenant_id = %s AND id = %s", (tenant_id, RECONCILER_ID))
    return Ctx(tx=tx, tenant_id=tenant_id, actor=actor, request_id=request_id or new_id("recon"))


def _each(rows: list[dict], fn) -> int:
    changed = 0
    for r in rows:
        try:
            with transaction() as tx:
                ctx = system_ctx(tx, r["tenant_id"])
                fn(ctx, r["id"])
                changed += bool(ctx.event_ids)
        except Exception:  # noqa: BLE001 - one bad contract must not stall the rest
            log.exception("reconcile failed for contract %s", r["id"])
    return changed


def run_once(tenant_id: str | None = None) -> dict:
    with transaction() as tx:
        expired = tx.all(
            """SELECT l.tenant_id, l.contract_id AS id FROM leases l
               WHERE l.status = 'active' AND l.expires_at <= clock_timestamp()
                 AND (%s::text IS NULL OR l.tenant_id = %s)""", (tenant_id, tenant_id))
        pending = tx.all(
            """SELECT tenant_id, id FROM contracts
               WHERE (status IN ('proposed','blocked','failed','rejected')
                      OR (stale_inputs AND status IN ('ready','leased','running','submitted','validating')))
                 AND (%s::text IS NULL OR tenant_id = %s)
               ORDER BY priority DESC, created_at""", (tenant_id, tenant_id))
    return {"leases_expired": _each(expired, contracts.expire_lease),
            "contracts_changed": _each(pending, contracts.reconcile_one)}
