"""Outbox delivery and its two in-process consumers: attention-policy notifications and metric
counters (spec §8 attention policy, §9 outbox, §13 metrics).

Delivery is at least once. Each consumer dedupes on (source, id) and tracks the highest
aggregate version it has seen, so a redelivered or late event cannot be applied twice."""
from __future__ import annotations

import logging
from typing import Callable

from cas.config import settings
from cas.persistence.db import Tx, transaction
from cas.service.core import new_id

log = logging.getLogger("cas.outbox")
LEVELS = {"ambient": 0, "digest": 1, "decision": 2, "interrupt": 3}

Handler = Callable[[Tx, dict, bool], None]


def consume(tx: Tx, consumer: str, event: dict, handler: Handler) -> str:
    """Apply one event to one consumer exactly once. Returns 'applied', 'duplicate' or 'stale'."""
    inserted = tx.one(
        """INSERT INTO inbox_dedup (consumer, source, event_id, tenant_id) VALUES (%s,%s,%s,%s)
           ON CONFLICT DO NOTHING RETURNING event_id""",
        (consumer, event["source"], event["id"], event["tenant_id"]))
    if inserted is None:
        return "duplicate"
    last = tx.scalar(
        """SELECT last_version FROM consumer_aggregate_versions
           WHERE consumer = %s AND tenant_id = %s AND aggregate_type = %s AND aggregate_id = %s FOR UPDATE""",
        (consumer, event["tenant_id"], event["aggregate_type"], event["aggregate_id"]))
    stale = last is not None and event["aggregate_version"] < last
    if not stale:
        tx.execute(
            """INSERT INTO consumer_aggregate_versions (consumer, tenant_id, aggregate_type, aggregate_id, last_version)
               VALUES (%s,%s,%s,%s,%s)
               ON CONFLICT (consumer, tenant_id, aggregate_type, aggregate_id)
               DO UPDATE SET last_version = EXCLUDED.last_version""",
            (consumer, event["tenant_id"], event["aggregate_type"], event["aggregate_id"], event["aggregate_version"]))
    handler(tx, event, stale)
    return "stale" if stale else "applied"


# ---------------------------------------------------------------- metrics consumer

METRIC_FOR_EVENT = {
    "cas.contract.accepted.v1": "contracts_accepted",
    "cas.contract.failed.v1": "contract_failures",
    "cas.contract.cancelled.v1": "contracts_cancelled",
    "cas.input.invalidated.v1": "stale_input_invalidations",
    "cas.policy.denied.v1": "policy_denials",
    "cas.decision.opened.v1": "human_escalations",
    "cas.effect.manual_recovery_required.v1": "manual_recoveries_required",
    "cas.artifact.rejected.v1": "artifact_versions_rejected",
}


def metrics_handler(tx: Tx, event: dict, stale: bool) -> None:
    metrics = [METRIC_FOR_EVENT[event["type"]]] if event["type"] in METRIC_FOR_EVENT else []
    if event["type"] == "cas.contract.rejected.v1":
        metrics.append("stale_result_rejections" if event["payload"].get("reason") == "stale_input"
                       else "validation_failures")
    if event["type"] == "cas.contract.ready.v1" and event["payload"].get("automatic"):
        metrics.append("automatic_retries")
    if not event["goal_id"]:
        return
    for metric in metrics:
        tx.execute(
            """INSERT INTO metrics_counters (tenant_id, goal_id, metric, value) VALUES (%s,%s,%s,1)
               ON CONFLICT (tenant_id, goal_id, metric)
               DO UPDATE SET value = metrics_counters.value + 1, updated_at = now()""",
            (event["tenant_id"], event["goal_id"], metric))


# ---------------------------------------------------------------- notification consumer

def _contract(tx: Tx, event: dict) -> dict | None:
    cid = event["payload"].get("contract_id") or (event["aggregate_id"] if event["aggregate_type"] == "contract" else None)
    if event["payload"].get("resource_type") == "contract":
        cid = event["payload"].get("resource_id")
    return tx.one("SELECT * FROM contracts WHERE tenant_id = %s AND id = %s", (event["tenant_id"], cid)) if cid else None


def _goal_owner(tx: Tx, event: dict) -> list[str]:
    row = tx.one("SELECT owner_principal_id FROM goals WHERE tenant_id = %s AND id = %s",
                 (event["tenant_id"], event["goal_id"]))
    return [row["owner_principal_id"]] if row else []


def _with_role(tx: Tx, event: dict, role: str) -> list[str]:
    return [r["principal_id"] for r in tx.all(
        """SELECT DISTINCT principal_id FROM policy_grants WHERE tenant_id = %s AND role = %s AND revoked_at IS NULL
             AND (resource_type = 'tenant' OR resource_id = %s)""",
        (event["tenant_id"], role, event["goal_id"]))]


def _route(tx: Tx, event: dict) -> tuple[str, list[str], bool, str, str] | None:
    """Attention policy: (level, recipients, mandated, why, summary) for an event, or None."""
    t, p = event["type"], event["payload"]
    c = _contract(tx, event)
    title = f"'{c['title']}'" if c else event["subject"]
    requester = [c["requester_id"]] if c else []
    escalation = [c["escalation_policy"]["decision_owner_id"]] if c else []
    if t == "cas.policy.denied.v1":
        return ("interrupt", _goal_owner(tx, event) + _with_role(tx, event, "admin") + escalation, True,
                "a security or policy boundary refused an action; these recipients are always told",
                f"Policy denied {p.get('action')} on {title}: {p.get('reason') or 'permission missing'}")
    if t == "cas.effect.manual_recovery_required.v1":
        return ("interrupt", requester + _goal_owner(tx, event), True,
                "a side effect has an unknown or failed outcome and cannot be retried automatically",
                f"Step '{p.get('step_key')}' of {title} needs manual recovery")
    if t == "cas.decision.opened.v1":
        return ("decision", [p["decision_owner_id"]], False, "you own this decision",
                f"Decision needed: {p.get('question')}")
    if t == "cas.contract.blocked.v1":
        return ("decision", requester + escalation, False,
                "work you requested or escalate for is blocked", f"{title} is blocked ({p.get('reason')})")
    if t in ("cas.contract.failed.v1", "cas.contract.rejected.v1"):
        return ("decision", requester, False, "work you requested did not complete",
                f"{title} {p.get('to_status')} ({p.get('reason')})")
    if t == "cas.goal.revision_proposed.v1":
        return ("decision", _goal_owner(tx, event), False, "a revision to your goal awaits your approval",
                f"Goal revision proposed by {p.get('proposed_by')}")
    if t == "cas.contract.submitted.v1":
        return ("digest", _with_role(tx, event, "validator"), False, "work is waiting for validation",
                f"{title} was submitted for validation")
    if t == "cas.contract.reassessment_required.v1":
        return ("digest", requester, False, "accepted or active work was built on something that changed",
                f"{title} needs reassessment")
    if t == "cas.contract.accepted.v1":
        return ("digest", requester + _goal_owner(tx, event), False, "work was accepted", f"{title} was accepted")
    if t == "cas.goal.revised.v1":
        return ("digest", _with_role(tx, event, "task_requester"), False, "the goal you work on changed",
                "The goal was revised")
    return None


def notification_handler(tx: Tx, event: dict, stale: bool) -> None:
    if stale or not event["goal_id"]:
        return
    route = _route(tx, event)
    if route is None:
        return
    level, recipients, mandated, why, summary = route
    for rid in dict.fromkeys(recipients):
        person = tx.one("SELECT kind, attention_preference FROM principals WHERE tenant_id = %s AND id = %s AND status = 'active'",
                        (event["tenant_id"], rid))
        if person is None or person["kind"] != "human":
            continue
        pref = person["attention_preference"]
        delivered, reason = level, why
        if mandated:
            if LEVELS[pref] < LEVELS[level]:
                reason = f"{why} (delivered as '{level}' despite the recipient's '{pref}' preference)"
        elif LEVELS[level] > LEVELS[pref]:
            delivered, reason = pref, f"{why} (lowered from '{level}' to '{pref}' by the recipient's preference)"
        tx.execute(
            """INSERT INTO notifications (tenant_id, id, recipient_id, goal_id, event_id, event_type, subject, level,
                   mandated, reason, summary) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
               ON CONFLICT (tenant_id, recipient_id, event_id) DO NOTHING""",
            (event["tenant_id"], new_id("ntf"), rid, event["goal_id"], event["id"], event["type"], event["subject"],
             delivered, mandated, reason, summary))


CONSUMERS: dict[str, Handler] = {"notifications": notification_handler, "metrics": metrics_handler}


# ---------------------------------------------------------------- dispatcher

def deliver_event(tx: Tx, event: dict, consumers: dict[str, Handler] | None = None) -> dict[str, str]:
    return {name: consume(tx, name, event, handler) for name, handler in (consumers or CONSUMERS).items()}


def dispatch_pending(limit: int = 200, consumers: dict[str, Handler] | None = None) -> dict:
    """Deliver pending outbox rows in order. A failed delivery is retried with backoff and
    dead-lettered after the configured number of attempts."""
    stats = {"delivered": 0, "failed": 0, "dead": 0}
    with transaction() as tx:
        seqs = [r["seq"] for r in tx.all(
            "SELECT seq FROM outbox WHERE status = 'pending' AND next_attempt_at <= clock_timestamp() ORDER BY seq LIMIT %s",
            (limit,))]
    for seq in seqs:
        with transaction() as tx:
            row = tx.one("SELECT * FROM outbox WHERE seq = %s AND status = 'pending' FOR UPDATE SKIP LOCKED", (seq,))
            if row is None:
                continue
            event = tx.one("SELECT * FROM events WHERE seq = %s", (row["event_seq"],))
            try:
                with tx.savepoint():
                    deliver_event(tx, event, consumers)
                tx.execute("UPDATE outbox SET status = 'delivered', delivered_at = now(), attempts = attempts + 1 WHERE seq = %s",
                           (seq,))
                stats["delivered"] += 1
            except Exception as exc:  # noqa: BLE001 - a consumer bug must not stop the queue
                log.exception("outbox delivery failed for event %s", event["id"])
                dead = row["attempts"] + 1 >= settings.outbox_max_attempts
                tx.execute(
                    """UPDATE outbox SET attempts = attempts + 1, last_error = %s, status = %s,
                              next_attempt_at = clock_timestamp() + make_interval(secs => least(300, power(2, attempts + 1)))
                       WHERE seq = %s""",
                    (str(exc)[:500], "dead" if dead else "pending", seq))
                stats["dead" if dead else "failed"] += 1
    return stats


def outbox_health(tx: Tx) -> dict:
    return tx.one(
        """SELECT count(*) FILTER (WHERE status = 'pending') AS pending,
                  count(*) FILTER (WHERE status = 'dead') AS dead_letter,
                  coalesce(extract(epoch FROM clock_timestamp() - min(created_at) FILTER (WHERE status = 'pending')), 0)::float
                    AS lag_seconds
           FROM outbox""")
