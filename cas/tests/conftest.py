"""Test fixtures. The suite runs against a real PostgreSQL: CAS_TEST_DATABASE_URL if set,
otherwise a throwaway embedded server. Each test gets its own tenant, so tests never share data."""
from __future__ import annotations

import os
import shutil
import tempfile
import uuid

import pytest
from fastapi.testclient import TestClient

from cas.config import settings


@pytest.fixture(scope="session", autouse=True)
def _database():
    tmp = tempfile.mkdtemp(prefix="cas-test-")
    settings.data_dir = type(settings.data_dir)(tmp)
    settings.blob_dir = os.path.join(tmp, "blobs")
    settings.retry_backoff_base_seconds = 0.0
    server = None
    if os.environ.get("CAS_TEST_DATABASE_URL"):
        settings.database_url = os.environ["CAS_TEST_DATABASE_URL"]
    else:
        import pgserver
        server = pgserver.get_server(os.path.join(tmp, "pg"))
        settings.database_url = server.get_uri()
    from cas.persistence import db
    db.migrate()
    yield
    db.close_pool()
    if server is not None:
        server.cleanup()
    shutil.rmtree(tmp, ignore_errors=True)


class Actor:
    """A principal and an HTTP client that authenticates as it."""

    def __init__(self, principal_id: str, credential: str, tenant_id: str):
        from cas.api.app import app
        self.id = principal_id
        self.tenant_id = tenant_id
        self.http = TestClient(app, raise_server_exceptions=False)
        self.http.headers["Authorization"] = f"Bearer {credential}"

    def get(self, path: str, **params):
        return self.http.get(path, params=params)

    def post(self, path: str, body: dict | None = None, *, version: int | None = None, key: str | None = None):
        headers = {"Idempotency-Key": key or uuid.uuid4().hex}
        if version is not None:
            headers["If-Match"] = f'"{version}"'
        return self.http.post(path, json=body or {}, headers=headers)

    def ok(self, path: str, body: dict | None = None, *, version: int | None = None, key: str | None = None) -> dict:
        r = self.post(path, body, version=version, key=key)
        assert r.status_code < 300, f"{path} -> {r.status_code} {r.text}"
        return r.json()["resource"]

    def read(self, path: str, **params) -> dict:
        r = self.get(path, **params)
        assert r.status_code == 200, f"{path} -> {r.status_code} {r.text}"
        return r.json()


class World:
    """One tenant with an admin, a goal owner, a validator and three agents."""

    SCOPES = ["staging:read", "staging:write"]

    def __init__(self):
        from cas.persistence import db
        from cas.service import principals
        self.tenant_id = "tenant_" + uuid.uuid4().hex[:12]
        with db.transaction() as tx:
            out = principals.bootstrap_tenant(tx, self.tenant_id, "Test tenant")
        self.admin = Actor(out["admin"]["id"], out["token"], self.tenant_id)
        self.owner = self.human("Product lead", ["goal_owner", "task_requester"], scopes=self.SCOPES)
        self.validator = self.human("Reviewer", ["validator"])
        self.agents = [self.agent(f"agent-{n}", [n_cap]) for n, n_cap in
                       (("designer", "spec"), ("engineer", "prototype"), ("analyst", "report"))]
        self.designer, self.engineer, self.analyst = self.agents
        self.goal_id = ""

    def human(self, name: str, roles: list[str], scopes: list[str] | None = None) -> Actor:
        p = self.admin.ok("/v1/principals", {"kind": "human", "display_name": name, "external_subject": uuid.uuid4().hex})
        actor = Actor(p["id"], p["token"], self.tenant_id)
        for role in roles:
            self.admin.ok("/v1/grants", {"principal_id": p["id"], "role": role, "scopes": scopes or []})
        return actor

    def agent(self, name: str, capabilities: list[str], scopes: list[str] | None = None, adapter_type: str = "pull",
              adapter_config_ref: str | None = None) -> Actor:
        a = self.admin.ok("/v1/agents", {
            "display_name": name, "external_subject": uuid.uuid4().hex,
            "capabilities": [{"capability_key": c, "adapter_type": adapter_type, "adapter_config_ref": adapter_config_ref}
                             for c in capabilities]})
        self.admin.ok("/v1/grants", {"principal_id": a["id"], "role": "task_owner",
                                     "scopes": self.SCOPES if scopes is None else scopes})
        return Actor(a["id"], a["token"], self.tenant_id)

    def goal(self, activate: bool = True, **overrides) -> dict:
        body = {"title": "Shared editing", "objective": "Ship conflict-safe shared editing",
                "success_criteria": ["no lost updates in staging"], "constraints": ["staging only"],
                "preferences": ["prefer small reversible steps"], "owner_principal_id": self.owner.id, **overrides}
        g = self.owner.ok("/v1/goals", body)
        self.goal_id = g["id"]
        self.admin.ok(f"/v1/goals/{g['id']}/resource-policy", {"allowed_scopes": self.SCOPES})
        if activate:
            g = self.owner.ok(f"/v1/goals/{g['id']}/activate", version=g["version"])
        return g

    def contract(self, title: str, *, deps: list[str] | None = None, inputs: list[dict] | None = None,
                 deliverable: str = "spec", checks: list[str] | None = None, scope: list[str] | None = None,
                 actor: Actor | None = None, **extra) -> dict:
        actor = actor or self.owner
        body = {"goal_id": self.goal_id, "title": title, "objective": f"Produce {title}", "requester_id": actor.id,
                "input_refs": inputs or [], "dependency_ids": deps or [],
                "deliverable_spec": [{"type": deliverable}], "acceptance_checks": checks or ["test:ok"],
                "permission_scope": scope or [],
                "escalation_policy": {"on_stale": "hold", "on_conflict": "reconcile_then_escalate",
                                      "decision_owner_id": self.owner.id},
                "max_attempts": 3, **extra}
        return actor.ok("/v1/contracts", body)

    def get(self, contract_id: str) -> dict:
        return self.owner.read(f"/v1/contracts/{contract_id}")

    def claim(self, agent: Actor, contract_id: str, lease_seconds: int = 60) -> str:
        c = agent.ok(f"/v1/contracts/{contract_id}/claim", {"worker_id": "w1", "lease_seconds": lease_seconds})
        return c["lease"]["lease_token"]

    def start(self, agent: Actor, contract_id: str) -> str:
        lease = self.claim(agent, contract_id)
        agent.ok(f"/v1/contracts/{contract_id}/start", {"lease_token": lease})
        return lease

    def produce(self, agent: Actor, contract_id: str, artifact_id: str, content: str, kind: str = "spec",
                lease: str | None = None) -> str:
        """Claim, start, register one artifact and submit. Returns the lease token."""
        lease = lease or self.start(agent, contract_id)
        agent.ok(f"/v1/contracts/{contract_id}/artifacts",
                 {"lease_token": lease, "artifact_id": artifact_id, "type": kind, "content_text": content,
                  "media_type": "text/plain"})
        agent.ok(f"/v1/contracts/{contract_id}/submit", {"lease_token": lease})
        return lease

    def accept(self, contract_id: str, check: str = "test:ok") -> dict:
        c = self.get(contract_id)
        return self.validator.ok(f"/v1/contracts/{contract_id}/accept",
                                 {"check_results": [{"check": check, "status": "pass"}]}, version=c["version"])

    def deliver(self, agent: Actor, contract_id: str, artifact_id: str, content: str, kind: str = "spec") -> dict:
        self.produce(agent, contract_id, artifact_id, content, kind)
        return self.accept(contract_id)

    def reconcile(self) -> dict:
        from cas.workers import reconciler
        return reconciler.run_once(self.tenant_id)

    def sql(self, statement: str, params: tuple = ()) -> list[dict]:
        from cas.persistence import db
        with db.transaction() as tx:
            cur = tx.execute(statement, params)
            return cur.fetchall() if cur.description else []

    def events(self, type_: str | None = None) -> list[dict]:
        items = self.owner.read(f"/v1/goals/{self.goal_id}/events", limit=200)["items"]
        return [e for e in items if type_ is None or e["type"] == type_]


@pytest.fixture
def world() -> World:
    w = World()
    w.goal()
    return w


def error_code(response) -> str:
    return response.json()["error"]["code"]
