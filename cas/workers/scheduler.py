"""The scheduler (spec §6, §10): gives ready contracts to capable, authorized agents that are
reachable through an adapter, then drives each run to submission or failure.

Agents registered with adapter_type 'pull' are never assigned here; they claim work themselves
through the API."""
from __future__ import annotations

import logging
import socket
from typing import Any

from cas.adapters.base import Adapter, RunRequest
from cas.domain.errors import CasError
from cas.persistence import blobstore
from cas.persistence.db import Tx, transaction
from cas.service import contracts, policy
from cas.service.core import Ctx, new_id
from cas.workers.reconciler import system_ctx

log = logging.getLogger("cas.scheduler")


class Scheduler:
    def __init__(self, adapters: dict[str, Adapter], configs: dict[str, dict], worker_id: str | None = None):
        self.adapters = adapters
        self.configs = configs
        self.worker_id = worker_id or f"scheduler@{socket.gethostname()}"

    # ------------------------------------------------------------ assignment

    def choose(self, tx: Tx, c: dict) -> tuple[str, dict] | None:
        """Pick an agent that is capable (a hint), authorized (a requirement) and has capacity."""
        needed = {d["type"] for d in c["deliverable_spec"]}
        caps = tx.all(
            """SELECT ac.agent_id, ac.capability_key, ac.adapter_type, ac.adapter_config_ref
               FROM agent_capabilities ac JOIN principals p ON p.tenant_id = ac.tenant_id AND p.id = ac.agent_id
               WHERE ac.tenant_id = %s AND p.status = 'active'
                 AND (%s::text IS NULL OR ac.agent_id = %s)""",
            (c["tenant_id"], c["assigned_owner_id"], c["assigned_owner_id"]))
        by_agent: dict[str, list[dict]] = {}
        for cap in caps:
            if cap["adapter_type"] in self.adapters:
                by_agent.setdefault(cap["agent_id"], []).append(cap)
        ranked = []
        for agent_id, agent_caps in by_agent.items():
            if not needed <= {cap["capability_key"] for cap in agent_caps}:
                continue
            if not contracts.can_hold(tx, c["tenant_id"], c, agent_id)[0]:
                continue
            if contracts.at_capacity(tx, c["tenant_id"], agent_id)[0]:
                continue
            load = tx.scalar("SELECT count(*) FROM leases WHERE tenant_id = %s AND holder_id = %s AND status = 'active'",
                             (c["tenant_id"], agent_id))
            cap = next(cap for cap in agent_caps if cap["capability_key"] in needed)
            ranked.append((load, agent_id, cap))
        if not ranked:
            return None
        _, agent_id, cap = min(ranked, key=lambda r: (r[0], r[1]))
        return agent_id, cap

    def assign_once(self, tenant_id: str | None = None) -> int:
        if not self.adapters:
            return 0
        with transaction() as tx:
            ready = tx.all(
                """SELECT c.tenant_id, c.id FROM contracts c JOIN goals g ON g.tenant_id = c.tenant_id AND g.id = c.goal_id
                   WHERE c.status = 'ready' AND g.status = 'active' AND (%s::text IS NULL OR c.tenant_id = %s)
                   ORDER BY c.priority DESC, c.created_at LIMIT 100""", (tenant_id, tenant_id))
        assigned = 0
        for r in ready:
            try:
                with transaction() as tx:
                    c = tx.one("SELECT * FROM contracts WHERE tenant_id = %s AND id = %s FOR UPDATE SKIP LOCKED",
                               (r["tenant_id"], r["id"]))
                    if c is None or c["status"] != "ready":
                        continue
                    choice = self.choose(tx, c)
                    if choice is None:
                        continue
                    agent_id, cap = choice
                    ctx = system_ctx(tx, c["tenant_id"])
                    leased, lease_token = contracts._grant_lease(ctx, c, agent_id, self.worker_id, None)
                    tx.execute(
                        """INSERT INTO adapter_runs (tenant_id, id, contract_id, fencing_token, agent_id, adapter_type,
                               lease_token, status, detail) VALUES (%s,%s,%s,%s,%s,%s,%s,'starting',%s)""",
                        (c["tenant_id"], new_id("arun"), c["id"], leased["fencing_token"], agent_id, cap["adapter_type"],
                         lease_token, {"adapter_config_ref": cap["adapter_config_ref"]}))
                    assigned += 1
            except Exception:  # noqa: BLE001
                log.exception("assignment failed for contract %s", r["id"])
        return assigned

    # ------------------------------------------------------------ run driving

    def _agent_ctx(self, tx: Tx, run: dict) -> Ctx:
        actor = tx.one("SELECT * FROM principals WHERE tenant_id = %s AND id = %s", (run["tenant_id"], run["agent_id"]))
        return Ctx(tx=tx, tenant_id=run["tenant_id"], actor=actor, request_id=new_id("run"))

    def _inputs(self, tx: Tx, c: dict) -> list[dict[str, Any]]:
        out = []
        for pin in c["pinned_inputs"]:
            v = tx.one("SELECT media_type FROM artifact_versions WHERE tenant_id = %s AND artifact_id = %s AND version_id = %s",
                       (c["tenant_id"], pin["artifact_id"], pin["version_id"]))
            data = blobstore.get(c["tenant_id"], pin["sha256"]) or b""
            try:
                content = {"content_text": data.decode()}
            except UnicodeDecodeError:
                import base64
                content = {"content_base64": base64.b64encode(data).decode()}
            out.append({**pin, "media_type": v["media_type"] if v else None, **content})
        return out

    def _set_run(self, tx: Tx, run: dict, status: str, **detail: Any) -> None:
        tx.execute(
            "UPDATE adapter_runs SET status = %s, external_run_id = %s, detail = detail || %s::jsonb, updated_at = now() WHERE tenant_id = %s AND id = %s",
            (status, detail.pop("external_run_id", run["external_run_id"]), detail, run["tenant_id"], run["id"]))

    def _fail(self, run: dict, reason: str, retryable: bool) -> None:
        with transaction() as tx:
            try:
                contracts.fail(self._agent_ctx(tx, run), run["contract_id"], run["lease_token"], reason[:500], retryable)
            except CasError as exc:
                log.info("could not fail contract %s for run %s: %s", run["contract_id"], run["id"], exc.code)
            self._set_run(tx, run, "failed", error=reason[:500])

    def _drive(self, run: dict) -> None:
        adapter = self.adapters.get(run["adapter_type"])
        if adapter is None:
            return
        if run["status"] == "cancelled":
            if run["external_run_id"]:
                adapter.cancel(run["external_run_id"])
            with transaction() as tx:
                self._set_run(tx, run, "cancelled", cancel_sent=True)
            return
        if run["status"] == "starting":
            with transaction() as tx:
                c = contracts.get(tx, run["tenant_id"], run["contract_id"])
                request = RunRequest(
                    contract={k: c[k] for k in ("id", "goal_id", "title", "objective", "deliverable_spec",
                                                "acceptance_checks", "permission_scope", "attempt")},
                    pinned_inputs=self._inputs(tx, c), lease_token=run["lease_token"],
                    policy_context={k: sorted(v) for k, v in policy.effective_scopes(
                        tx, run["tenant_id"], c, run["agent_id"]).items()},
                    config=self.configs.get(run["detail"].get("adapter_config_ref") or "", {}))
            try:
                external_id = adapter.start(request)
            except Exception as exc:  # noqa: BLE001
                return self._fail(run, f"adapter failed to start: {exc}", retryable=False)
            with transaction() as tx:
                contracts.start(self._agent_ctx(tx, run), run["contract_id"], run["lease_token"])
                self._set_run(tx, run, "running", external_run_id=external_id)
            return
        with transaction() as tx:
            contracts.heartbeat(self._agent_ctx(tx, run), run["contract_id"], run["lease_token"], None)
        result = adapter.poll(run["external_run_id"])
        if result.state == "progress":
            return
        if result.state == "error":
            return self._fail(run, result.detail or "run failed", result.retryable)
        try:
            refs = adapter.collect(run["external_run_id"])
            with transaction() as tx:
                ctx = self._agent_ctx(tx, run)
                for ref in refs:
                    contracts.register_artifact(ctx, run["contract_id"], run["lease_token"], ref)
                contracts.submit(ctx, run["contract_id"], run["lease_token"])
                self._set_run(tx, run, "finished")
        except CasError as exc:
            self._fail(run, f"could not submit results: {exc.code}: {exc.message}", retryable=False)
        except Exception as exc:  # noqa: BLE001
            self._fail(run, f"could not collect results: {exc}", retryable=False)
        finally:
            cleanup = getattr(adapter, "cleanup", None)
            if cleanup:
                cleanup(run["external_run_id"])

    def drive_once(self, tenant_id: str | None = None) -> int:
        with transaction() as tx:
            runs = tx.all(
                """SELECT * FROM adapter_runs
                   WHERE (status IN ('starting','running') OR (status = 'cancelled' AND NOT detail ? 'cancel_sent'))
                     AND adapter_type = ANY(%s::text[]) AND (%s::text IS NULL OR tenant_id = %s) ORDER BY created_at""",
                ("{" + ",".join(self.adapters) + "}", tenant_id, tenant_id))
        for run in runs:
            try:
                self._drive(run)
            except CasError as exc:
                # The lease was fenced or expired under us: stop the run; the reconciler owns recovery.
                log.info("run %s stopped: %s", run["id"], exc.code)
                if run["external_run_id"]:
                    self.adapters[run["adapter_type"]].cancel(run["external_run_id"])
                with transaction() as tx:
                    self._set_run(tx, run, "cancelled", cancel_sent=True, error=exc.code)
            except Exception:  # noqa: BLE001
                log.exception("driving run %s failed", run["id"])
        return len(runs)
