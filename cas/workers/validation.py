"""The validation worker (spec §3 submitted -> validating). It runs the checks a machine can
decide and leaves everything else, including every 'review:' check, to a human validator."""
from __future__ import annotations

import hashlib
import json
import logging
from typing import Callable

from jsonschema import Draft202012Validator

from cas.domain.schemas import SCHEMA_DIR
from cas.persistence import blobstore
from cas.persistence.db import Tx, transaction
from cas.service import artifacts, contracts
from cas.workers.reconciler import system_ctx

log = logging.getLogger("cas.validation")
Check = Callable[[Tx, dict, list[dict], str], tuple[bool, dict]]


def _check_hash(tx: Tx, c: dict, versions: list[dict], arg: str) -> tuple[bool, dict]:
    bad = []
    for v in versions:
        data = blobstore.get(c["tenant_id"], v["sha256"])
        if data is None or hashlib.sha256(data).hexdigest() != v["sha256"]:
            bad.append(f"{v['artifact_id']}@{v['version_id']}")
    return not bad, {"mismatched": bad}


def _check_nonempty(tx: Tx, c: dict, versions: list[dict], arg: str) -> tuple[bool, dict]:
    empty = [f"{v['artifact_id']}@{v['version_id']}" for v in versions if v["size_bytes"] == 0]
    return bool(versions) and not empty, {"empty": empty}


def _check_json(tx: Tx, c: dict, versions: list[dict], arg: str) -> tuple[bool, dict]:
    """json:<type> - every artifact of that type parses as JSON. json:<type>:<schema file> also
    validates it against a schema in cas/docs/schemas."""
    artifact_type, _, schema_name = arg.partition(":")
    schema = None
    if schema_name:
        path = (SCHEMA_DIR / schema_name).resolve()
        if SCHEMA_DIR not in path.parents or not path.exists():
            return False, {"error": f"unknown schema '{schema_name}'"}
        schema = Draft202012Validator(json.loads(path.read_text()))
    problems, matched = [], 0
    for v in versions:
        if v["artifact_type"] != artifact_type:
            continue
        matched += 1
        try:
            doc = json.loads(blobstore.get(c["tenant_id"], v["sha256"]) or b"")
        except ValueError:
            problems.append(f"{v['artifact_id']}@{v['version_id']}: not JSON")
            continue
        if schema:
            problems += [f"{v['artifact_id']}@{v['version_id']}: {e.message}" for e in schema.iter_errors(doc)][:5]
    return matched > 0 and not problems, {"problems": problems, "artifacts_checked": matched}


AUTOMATED: dict[str, Check] = {"hash": _check_hash, "nonempty": _check_nonempty, "json": _check_json}


def automated(check: str) -> Check | None:
    return AUTOMATED.get(check.partition(":")[0])


def validate_one(tenant_id: str, contract_id: str) -> str:
    with transaction() as tx:
        ctx = system_ctx(tx, tenant_id)
        c = contracts.get(tx, tenant_id, contract_id, lock=True)
        if c["status"] not in ("submitted", "validating") or c["stale_inputs"]:
            return "skipped"
        recorded = contracts.check_status(tx, tenant_id, c)
        todo = [k for k, v in recorded.items() if v is None and automated(k)]
        manual = [k for k, v in recorded.items() if v is None and not automated(k)]
        if not todo and (manual or c["status"] == "validating"):
            return "waiting_for_human" if manual else "skipped"
        versions = [v for v in artifacts.versions_for_contract(tx, tenant_id, c["id"])
                    if v["fencing_token"] == c["fencing_token"] and v["status"] == "proposed"]
        results = []
        for key in todo:
            ok, evidence = automated(key)(tx, c, versions, key.partition(":")[2])
            results.append({"check": key, "status": "pass" if ok else "fail", "evidence": evidence})
        if any(r["status"] == "fail" for r in results):
            contracts.reject(ctx, c["id"], "automated check failed", results, None)
            return "rejected"
        if manual:
            contracts.record_checks(ctx, c["id"], results)
            return "waiting_for_human"
        contracts.accept(ctx, c["id"], results, None)
        return "accepted"


def run_once(tenant_id: str | None = None) -> dict:
    with transaction() as tx:
        rows = tx.all(
            """SELECT tenant_id, id FROM contracts WHERE status IN ('submitted','validating') AND NOT stale_inputs
                 AND (%s::text IS NULL OR tenant_id = %s) ORDER BY priority DESC, updated_at
               FOR UPDATE SKIP LOCKED""", (tenant_id, tenant_id))
    stats: dict[str, int] = {}
    for r in rows:
        try:
            outcome = validate_one(r["tenant_id"], r["id"])
        except Exception:  # noqa: BLE001
            log.exception("validation failed for contract %s", r["id"])
            outcome = "error"
        stats[outcome] = stats.get(outcome, 0) + 1
    return stats
