"""Conformance scenarios T01-T18 from spec §12, driven through the HTTP API."""
from __future__ import annotations

import threading
import uuid

from cas.tests.conftest import World, error_code

LATEST = lambda artifact_id: {"artifact_id": artifact_id, "required_version": "latest", "mode": "latest_accepted"}  # noqa: E731


def test_t01_downstream_waits_for_upstream(world: World):
    up = world.contract("design")
    down = world.contract("build", deps=[up["id"]], inputs=[LATEST("design_spec")], deliverable="prototype")
    assert up["status"] == "ready"
    assert down["status"] == "proposed"

    world.produce(world.designer, up["id"], "design_spec", "spec v1")
    world.reconcile()
    assert world.get(up["id"])["status"] == "submitted"
    detail = world.get(down["id"])
    assert detail["status"] == "proposed"
    assert detail["readiness"]["reason"] == "dependencies_unmet"
    # A contract that is not ready cannot be claimed.
    r = world.engineer.post(f"/v1/contracts/{down['id']}/claim", {"worker_id": "w"})
    assert r.status_code == 409 and error_code(r) == "INVALID_TRANSITION"


def test_t02_accepting_upstream_readies_downstream_and_pins_v1(world: World):
    up = world.contract("design")
    down = world.contract("build", deps=[up["id"]], inputs=[LATEST("design_spec")], deliverable="prototype")
    world.deliver(world.designer, up["id"], "design_spec", "spec v1")

    detail = world.get(down["id"])
    assert detail["status"] == "ready"
    assert [(p["artifact_id"], p["version_id"]) for p in detail["pinned_inputs"]] == [("design_spec", "v1")]
    artifact = world.owner.read("/v1/artifacts/design_spec")
    assert artifact["current_version_id"] == "v1"
    assert detail["pinned_inputs"][0]["sha256"] == artifact["versions"][0]["sha256"]


def test_t03_supersession_fences_running_downstream(world: World):
    up = world.contract("design")
    down = world.contract("build", deps=[up["id"]], inputs=[LATEST("design_spec")], deliverable="prototype")
    world.deliver(world.designer, up["id"], "design_spec", "spec v1")
    lease = world.start(world.engineer, down["id"])
    world.engineer.ok(f"/v1/contracts/{down['id']}/artifacts",
                      {"lease_token": lease, "artifact_id": "proto", "type": "prototype", "content_text": "built on v1"})

    # Rework of accepted upstream happens in a successor contract, which publishes v2.
    accepted = world.get(up["id"])
    successor = world.owner.ok(f"/v1/contracts/{up['id']}/revise", {"rationale": "interaction changed"},
                               version=accepted["version"])
    assert successor["supersedes_contract_id"] == up["id"] and successor["status"] == "ready"
    world.deliver(world.designer, successor["id"], "design_spec", "spec v2")

    detail = world.get(down["id"])
    assert detail["status"] == "blocked" and detail["blocked_reason"] == "stale_input"
    assert detail["attempts"][-1]["status"] == "fenced"
    # The running attempt is fenced: nothing it produced can be published or accepted.
    r = world.engineer.post(f"/v1/contracts/{down['id']}/submit", {"lease_token": lease})
    assert r.status_code == 409 and error_code(r) == "LEASE_FENCED"
    assert [v["status"] for v in world.owner.read("/v1/artifacts/proto")["versions"]] == ["rejected"]
    r = world.validator.post(f"/v1/contracts/{down['id']}/accept", {"check_results": [{"check": "test:ok", "status": "pass"}]},
                             version=detail["version"])
    assert r.status_code == 409
    invalidated = world.events("cas.input.invalidated.v1")
    assert invalidated and invalidated[0]["data"]["old_version"] == "v1" and invalidated[0]["data"]["new_version"] == "v2"
    # The accepted upstream stays a historical fact.
    assert world.get(up["id"])["status"] == "accepted"

    # Explicit revalidation re-pins to v2.
    again = world.owner.ok(f"/v1/contracts/{down['id']}/revalidate", version=detail["version"])
    assert again["status"] == "ready" and again["pinned_inputs"][0]["version_id"] == "v2"


def test_t04_two_claimers_race_one_lease(world: World):
    c = world.contract("design")
    racer = world.agent("agent-second-designer", ["spec"])
    results, barrier = [], threading.Barrier(2)

    def go(agent):
        barrier.wait()
        results.append(agent.post(f"/v1/contracts/{c['id']}/claim", {"worker_id": agent.id}))

    threads = [threading.Thread(target=go, args=(a,)) for a in (world.designer, racer)]
    [t.start() for t in threads]
    [t.join() for t in threads]

    assert sorted(r.status_code for r in results) == [200, 409]
    loser = next(r for r in results if r.status_code == 409)
    assert error_code(loser) == "INVALID_TRANSITION"
    leases = world.sql("SELECT status, fencing_token FROM leases WHERE tenant_id = %s AND contract_id = %s",
                       (world.tenant_id, c["id"]))
    assert leases == [{"status": "active", "fencing_token": 1}]


def test_t05_expired_lease_is_replaced_and_old_token_rejected(world: World):
    c = world.contract("design")
    old = world.start(world.designer, c["id"])
    world.designer.ok(f"/v1/contracts/{c['id']}/artifacts",
                      {"lease_token": old, "artifact_id": "design_spec", "type": "spec", "content_text": "late work"})
    world.sql("UPDATE leases SET expires_at = now() - interval '1 second' WHERE tenant_id = %s AND contract_id = %s",
              (world.tenant_id, c["id"]))

    r = world.designer.post(f"/v1/contracts/{c['id']}/heartbeat", {"lease_token": old})
    assert r.status_code == 409 and error_code(r) == "LEASE_EXPIRED"
    world.reconcile()
    detail = world.get(c["id"])
    assert detail["status"] == "ready" and detail["attempt"] == 2  # failed, then retried within budget
    assert [a["status"] for a in detail["attempts"]] == ["failed"]

    new = world.claim(world.designer, c["id"])
    assert new != old and world.get(c["id"])["fencing_token"] == 2
    r = world.designer.post(f"/v1/contracts/{c['id']}/submit", {"lease_token": old})
    assert r.status_code == 409 and error_code(r) == "LEASE_FENCED"
    r = world.designer.post(f"/v1/contracts/{c['id']}/artifacts",
                            {"lease_token": old, "artifact_id": "design_spec", "type": "spec", "content_text": "x"})
    assert r.status_code == 409 and error_code(r) == "LEASE_FENCED"
    assert world.get(c["id"])["status"] == "leased"


def test_t06_idempotent_submit(world: World):
    c = world.contract("design")
    lease = world.start(world.designer, c["id"])
    world.designer.ok(f"/v1/contracts/{c['id']}/artifacts",
                      {"lease_token": lease, "artifact_id": "design_spec", "type": "spec", "content_text": "spec"})
    key = uuid.uuid4().hex
    first = world.designer.post(f"/v1/contracts/{c['id']}/submit", {"lease_token": lease}, key=key)
    again = world.designer.post(f"/v1/contracts/{c['id']}/submit", {"lease_token": lease}, key=key)
    assert first.status_code == again.status_code == 200
    assert first.json() == again.json()
    assert again.headers["Idempotent-Replay"] == "true"
    assert len(world.events("cas.contract.submitted.v1")) == 1

    different = world.designer.post(f"/v1/contracts/{c['id']}/submit", {"lease_token": "another-token"}, key=key)
    assert different.status_code == 409 and error_code(different) == "IDEMPOTENCY_KEY_REUSED"
    missing = world.designer.http.post(f"/v1/contracts/{c['id']}/submit", json={"lease_token": lease})
    assert missing.status_code == 422 and error_code(missing) == "IDEMPOTENCY_KEY_REQUIRED"


def test_t07_outdated_if_match_does_not_overwrite(world: World):
    up = world.contract("design")
    c = world.contract("build", deps=[up["id"]], deliverable="prototype")
    v = c["version"]
    first = world.owner.ok(f"/v1/contracts/{c['id']}/revise", {"changes": {"title": "first writer"}}, version=v)
    assert first["version"] == v + 1
    stale = world.owner.post(f"/v1/contracts/{c['id']}/revise", {"changes": {"title": "second writer"}}, version=v)
    assert stale.status_code == 409 and error_code(stale) == "VERSION_CONFLICT"
    assert stale.json()["error"]["details"]["current_version"] == v + 1
    assert world.get(c["id"])["title"] == "first writer"
    missing = world.owner.post(f"/v1/contracts/{c['id']}/revise", {"changes": {"title": "no precondition"}})
    assert missing.status_code == 428


def test_t08_cycle_is_rejected_without_changing_the_graph(world: World):
    a = world.contract("a")
    b = world.contract("b", deps=[a["id"]])
    c = world.contract("c", deps=[b["id"]])
    before = world.sql("SELECT upstream_id, downstream_id FROM contract_dependencies WHERE tenant_id = %s ORDER BY 1, 2",
                       (world.tenant_id,))
    version = world.get(a["id"])["version"]
    r = world.owner.post(f"/v1/contracts/{a['id']}/dependencies", {"upstream_id": c["id"]}, version=version)
    assert r.status_code == 422 and error_code(r) == "DEPENDENCY_CYCLE"
    r = world.owner.post(f"/v1/contracts/{a['id']}/dependencies", {"upstream_id": a["id"]}, version=version)
    assert r.status_code == 422 and error_code(r) == "DEPENDENCY_CYCLE"
    after = world.sql("SELECT upstream_id, downstream_id FROM contract_dependencies WHERE tenant_id = %s ORDER BY 1, 2",
                      (world.tenant_id,))
    assert after == before
    assert world.get(a["id"])["version"] == version and world.get(a["id"])["status"] == "ready"


def test_t09_missing_permission_denies_side_effect_whatever_the_goal_says():
    world = World()
    world.goal(preferences=["deploying to production as early as possible is useful"])
    c = world.contract("build", deliverable="prototype", scope=["staging:write"])
    lease = world.start(world.engineer, c["id"])

    ok = world.engineer.ok(f"/v1/contracts/{c['id']}/effects",
                           {"lease_token": lease, "step_key": "deploy-staging", "scope": "staging:write", "idempotent": True})
    assert ok["status"] == "authorized"

    r = world.engineer.post(f"/v1/contracts/{c['id']}/effects",
                            {"lease_token": lease, "step_key": "deploy-prod", "scope": "prod:deploy"})
    assert r.status_code == 403 and error_code(r) == "POLICY_DENIED"
    assert set(r.json()["error"]["details"]["missing_in"]) == {"delegator", "agent", "task", "resource"}
    assert not world.sql("SELECT 1 FROM external_effects WHERE tenant_id = %s AND step_key = 'deploy-prod'", (world.tenant_id,))

    # The denial is audited, visible on the goal, and escalated to a human...
    audit = world.admin.read("/v1/audit", goal_id=world.goal_id, decision="deny")["items"]
    assert audit and audit[0]["action"] == "effect.authorize" and audit[0]["actor_id"] == world.engineer.id
    assert world.events("cas.policy.denied.v1")
    queue = world.owner.read(f"/v1/goals/{world.goal_id}/exceptions")["items"]
    denial = next(i for i in queue if i["kind"] == "permission_denied")
    assert denial["status"] == "awaiting_authority"
    # ...whose decision grants nothing: the same action is still refused afterwards.
    world.owner.ok(f"/v1/decisions/{denial['decision_id']}/resolve",
                   {"chosen_alternative": "request_grant", "rationale": "we do want this"}, version=denial["version"])
    r = world.engineer.post(f"/v1/contracts/{c['id']}/effects",
                            {"lease_token": lease, "step_key": "deploy-prod", "scope": "prod:deploy"})
    assert r.status_code == 403
    # A scope the agent holds but the contract does not carry is refused too (intersection, not union).
    world.admin.ok("/v1/grants", {"principal_id": world.engineer.id, "role": "task_owner", "scopes": ["prod:deploy"]})
    r = world.engineer.post(f"/v1/contracts/{c['id']}/effects",
                            {"lease_token": lease, "step_key": "deploy-prod", "scope": "prod:deploy"})
    assert r.status_code == 403 and "agent" not in r.json()["error"]["details"]["missing_in"]
    # An agent cannot grant itself anything.
    r = world.engineer.post("/v1/grants", {"principal_id": world.engineer.id, "role": "task_owner", "scopes": ["prod:deploy"]})
    assert r.status_code == 403


def test_t10_agent_goal_revision_needs_owner_approval(world: World):
    goal = world.owner.read(f"/v1/goals/{world.goal_id}")
    proposed = world.engineer.ok(f"/v1/goals/{world.goal_id}/revisions",
                                 {"changes": {"success_criteria": ["no lost updates", "p95 merge under 200 ms"]},
                                  "rationale": "latency matters to users"})
    rev = proposed["revision"]
    assert rev["status"] == "proposed"
    unchanged = world.owner.read(f"/v1/goals/{world.goal_id}")
    assert unchanged["version"] == goal["version"] and unchanged["success_criteria"] == goal["success_criteria"]

    r = world.engineer.post(f"/v1/goals/{world.goal_id}/revisions/{rev['id']}/approve", {}, version=goal["version"])
    assert r.status_code == 403
    r = world.admin.post(f"/v1/goals/{world.goal_id}/revisions/{rev['id']}/approve", {}, version=goal["version"])
    assert r.status_code == 403  # not even an admin; only the goal's owner

    approved = world.owner.ok(f"/v1/goals/{world.goal_id}/revisions/{rev['id']}/approve", {"note": "agreed"},
                              version=goal["version"])
    assert approved["version"] == goal["version"] + 1
    assert approved["success_criteria"] == ["no lost updates", "p95 merge under 200 ms"]
    assert approved["revision"]["status"] == "applied" and approved["revision"]["decided_by"] == world.owner.id


def test_t11_reject_then_retry_preserves_the_failed_version(world: World):
    c = world.contract("design")
    world.produce(world.designer, c["id"], "design_spec", "first try")
    submitted = world.get(c["id"])
    rejected = world.validator.ok(f"/v1/contracts/{c['id']}/reject",
                                  {"reason": "misses the conflict case",
                                   "check_results": [{"check": "test:ok", "status": "fail", "evidence": {"case": "conflict"}}]},
                                  version=submitted["version"])
    assert rejected["status"] == "rejected" and rejected["attempt"] == 1

    retried = world.owner.ok(f"/v1/contracts/{c['id']}/retry", version=rejected["version"])
    assert retried["status"] == "ready" and retried["attempt"] == 2
    world.deliver(world.designer, c["id"], "design_spec", "second try")

    detail = world.get(c["id"])
    assert detail["status"] == "accepted"
    assert [(v["version_id"], v["status"]) for v in detail["artifacts"]] == [("v1", "rejected"), ("v2", "accepted")]
    assert [(a["attempt_no"], a["status"]) for a in detail["attempts"]] == [(1, "rejected"), (2, "accepted")]
    assert [(h["check_key"], h["status"]) for h in detail["check_history"]] == [("test:ok", "fail"), ("test:ok", "pass")]
    actions = [a["action"] for a in world.admin.read("/v1/audit", goal_id=world.goal_id, limit=200)["items"]]
    assert "contract.reject" in actions and "contract.retry" in actions
    # The rejected bytes are still retrievable.
    r = world.owner.get("/v1/artifacts/design_spec/versions/v1/content")
    assert r.status_code == 200 and r.content == b"first try"


def test_t12_conflict_beyond_two_rounds_becomes_a_decision_and_holds_dependents(world: World):
    a = world.contract("option a")
    b = world.contract("option b")
    dependent = world.contract("build", deps=[a["id"]], deliverable="prototype")
    second = world.agent("agent-second-designer", ["spec"])
    la, lb = world.start(world.designer, a["id"]), world.start(second, b["id"])

    def propose(agent, cid, lease, artifact, text):
        return agent.ok(f"/v1/contracts/{cid}/artifacts",
                        {"lease_token": lease, "artifact_id": artifact, "type": "spec", "content_text": text})["version_id"]

    pa, pb = propose(world.designer, a["id"], la, "spec_a", "locks"), propose(second, b["id"], lb, "spec_b", "crdt")
    refs = lambda va, vb: [{"artifact_id": "spec_a", "version_id": va}, {"artifact_id": "spec_b", "version_id": vb}]  # noqa: E731
    conflict = world.designer.ok("/v1/conflicts", {
        "goal_id": world.goal_id, "kind": "artifact_incompatibility", "contract_ids": [a["id"], b["id"]],
        "proposals": refs(pa, pb), "description": "Locking and CRDT designs are incompatible"})
    assert conflict["status"] == "open" and conflict["max_rounds"] == 2

    # A round with nothing new does not count.
    r = world.designer.post(f"/v1/conflicts/{conflict['id']}/rounds", {"proposals": refs(pa, pb)}, version=conflict["version"])
    assert r.status_code == 422 and error_code(r) == "NO_NEW_EVIDENCE"

    pa = propose(world.designer, a["id"], la, "spec_a", "locks, revised")
    conflict = world.designer.ok(f"/v1/conflicts/{conflict['id']}/rounds", {"proposals": refs(pa, pb)}, version=conflict["version"])
    pb = propose(second, b["id"], lb, "spec_b", "crdt, revised")
    conflict = second.ok(f"/v1/conflicts/{conflict['id']}/rounds", {"proposals": refs(pa, pb)}, version=conflict["version"])
    assert (conflict["status"], conflict["rounds"]) == ("open", 2)
    assert world.get(dependent["id"])["status"] == "proposed"

    pa = propose(world.designer, a["id"], la, "spec_a", "locks, revised again")
    conflict = world.designer.ok(f"/v1/conflicts/{conflict['id']}/rounds", {"proposals": refs(pa, pb)}, version=conflict["version"])
    assert conflict["status"] == "escalated" and conflict["rounds"] == 2

    decision = world.owner.read(f"/v1/decisions/{conflict['decision_id']}")
    assert decision["state"] == "open" and decision["decision_owner_id"] == world.owner.id
    assert len(decision["alternatives"]) == 2 and dependent["id"] in decision["held_contract_ids"]
    held = world.get(dependent["id"])
    assert held["status"] == "blocked" and held["blocked_reason"] == "decision_hold"

    # While the decision is open, neither side can be accepted.
    world.designer.ok(f"/v1/contracts/{a['id']}/submit", {"lease_token": la})
    r = world.validator.post(f"/v1/contracts/{a['id']}/accept", {"check_results": [{"check": "test:ok", "status": "pass"}]},
                             version=world.get(a["id"])["version"])
    assert r.status_code == 409 and error_code(r) == "DECISION_PENDING"

    world.owner.ok(f"/v1/decisions/{decision['id']}/resolve",
                   {"chosen_alternative": f"spec_a@{pa}", "rationale": "simpler to operate"}, version=decision["version"])
    assert world.owner.read(f"/v1/conflicts/{conflict['id']}")["status"] == "resolved"
    world.accept(a["id"])
    assert world.get(dependent["id"])["status"] == "ready"


def test_t13_decision_against_stale_evidence_is_refused(world: World):
    world.owner.ok(f"/v1/goals/{world.goal_id}/artifacts",
                   {"artifact_id": "interaction_spec", "type": "spec", "content_text": "v1 of the spec"})
    c = world.contract("build", inputs=[LATEST("interaction_spec")], deliverable="prototype")
    decision = world.engineer.ok("/v1/decisions", {
        "goal_id": world.goal_id, "contract_ids": [c["id"]], "question": "Ship with the v1 interaction model?",
        "kind": "preference", "decision_owner_id": world.owner.id,
        "alternatives": [{"id": "ship", "label": "Ship"}, {"id": "wait", "label": "Wait"}],
        "evidence_refs": [{"artifact_id": "interaction_spec", "version_id": "v1"}]})
    assert world.get(c["id"])["status"] == "blocked"

    world.owner.ok(f"/v1/goals/{world.goal_id}/artifacts",
                   {"artifact_id": "interaction_spec", "type": "spec", "content_text": "v2 of the spec"})
    r = world.owner.post(f"/v1/decisions/{decision['id']}/resolve", {"chosen_alternative": "ship", "rationale": "ok"},
                         version=decision["version"])
    assert r.status_code == 409 and error_code(r) == "DECISION_BASELINE_CHANGED"
    current = world.owner.read(f"/v1/decisions/{decision['id']}")
    assert current["state"] == "open" and current["baseline_current"] is False
    assert world.get(c["id"])["status"] == "blocked"

    refreshed = world.owner.ok(f"/v1/decisions/{decision['id']}/refresh",
                               {"evidence_refs": [{"artifact_id": "interaction_spec", "version_id": "v2"}]},
                               version=current["version"])
    resolved = world.owner.ok(f"/v1/decisions/{decision['id']}/resolve", {"chosen_alternative": "ship", "rationale": "v2 is fine"},
                              version=refreshed["version"])
    assert resolved["state"] == "resolved"
    ready = world.get(c["id"])
    assert ready["status"] == "ready" and ready["pinned_inputs"][0]["version_id"] == "v2"


def test_t14_inputs_invalidated_during_validation_block_acceptance(world: World):
    world.owner.ok(f"/v1/goals/{world.goal_id}/artifacts",
                   {"artifact_id": "interaction_spec", "type": "spec", "content_text": "v1"})
    c = world.contract("build", inputs=[LATEST("interaction_spec")], deliverable="prototype")
    world.produce(world.engineer, c["id"], "proto", "built on v1", kind="prototype")
    world.owner.ok(f"/v1/goals/{world.goal_id}/artifacts",
                   {"artifact_id": "interaction_spec", "type": "spec", "content_text": "v2"})

    detail = world.get(c["id"])
    assert detail["status"] == "submitted" and detail["stale_inputs"] is True
    r = world.validator.post(f"/v1/contracts/{c['id']}/accept", {"check_results": [{"check": "test:ok", "status": "pass"}]},
                             version=detail["version"])
    assert r.status_code == 409 and error_code(r) == "STALE_INPUT"
    assert world.owner.read("/v1/artifacts/proto")["current_version_id"] is None

    world.reconcile()
    detail = world.get(c["id"])
    assert detail["status"] == "rejected" and detail["status_reason"] == "stale_input"
    assert [v["status"] for v in detail["artifacts"]] == ["rejected"]


def test_t15_outbox_redelivery_is_applied_once(world: World):
    from cas.persistence import db
    from cas.service import notifications

    c = world.contract("design")
    world.deliver(world.designer, c["id"], "design_spec", "spec")
    notifications.dispatch_pending(limit=10_000)

    def state():
        metrics = world.owner.read(f"/v1/goals/{world.goal_id}/overview")["metrics"]
        notes = world.sql("SELECT count(*) AS n FROM notifications WHERE tenant_id = %s", (world.tenant_id,))[0]["n"]
        return metrics, notes

    before = state()
    assert before[0]["contracts_accepted"] == 1 and before[1] > 0

    # Redeliver everything this tenant ever emitted, as an at-least-once transport may.
    world.sql("UPDATE outbox SET status = 'pending', next_attempt_at = now() WHERE tenant_id = %s", (world.tenant_id,))
    stats = notifications.dispatch_pending(limit=10_000)
    assert stats["delivered"] > 0 and state() == before

    with db.transaction() as tx:
        event = tx.one("SELECT * FROM events WHERE tenant_id = %s AND type = 'cas.contract.accepted.v1'", (world.tenant_id,))
        assert notifications.deliver_event(tx, event) == {"notifications": "duplicate", "metrics": "duplicate"}
        # An older version arriving late is recognised as stale by its aggregate version.
        late = dict(event, id="evt_late_" + uuid.uuid4().hex, aggregate_version=event["aggregate_version"] - 1)
        seen = []
        assert notifications.consume(tx, "notifications", late, lambda _tx, _e, stale: seen.append(stale)) == "stale"
        assert seen == [True]
    assert state() == before


def test_t16_partial_external_failure_retries_only_idempotent_steps():
    world = World()
    world.goal()
    c = world.contract("release", deliverable="prototype", scope=["staging:write"])
    lease = world.start(world.engineer, c["id"])

    def step(key: str, idempotent: bool, lease_token: str) -> dict:
        return world.engineer.ok(f"/v1/contracts/{c['id']}/effects",
                                 {"lease_token": lease_token, "step_key": key, "scope": "staging:write", "idempotent": idempotent})

    def report(effect: dict, status: str, lease_token: str) -> dict:
        return world.engineer.ok(f"/v1/contracts/{c['id']}/effects/{effect['id']}/result",
                                 {"lease_token": lease_token, "status": status})

    assert report(step("upload-bundle", True, lease), "succeeded", lease)["status"] == "succeeded"
    assert report(step("warm-cache", True, lease), "failed", lease)["status"] == "failed"
    charge = report(step("notify-customers", False, lease), "failed", lease)
    assert charge["status"] == "manual_recovery_required"
    failed = world.engineer.ok(f"/v1/contracts/{c['id']}/fail", {"lease_token": lease, "reason": "partial failure", "retryable": True})
    assert failed["status"] == "failed"  # no automatic retry while a step needs manual recovery

    world.reconcile()
    assert world.get(c["id"])["status"] == "failed"
    queue = world.owner.read(f"/v1/goals/{world.goal_id}/exceptions")["items"]
    assert any(i["kind"] == "manual_recovery" and i["effect_id"] == charge["id"] for i in queue)
    r = world.owner.post(f"/v1/contracts/{c['id']}/retry", version=world.get(c["id"])["version"])
    assert r.status_code == 409 and error_code(r) == "MANUAL_RECOVERY_REQUIRED"
    r = world.engineer.post(f"/v1/effects/{charge['id']}/recover", {"outcome": "not_applied", "note": "nothing sent"})
    assert r.status_code == 403  # recovery is a human call

    world.owner.ok(f"/v1/effects/{charge['id']}/recover", {"outcome": "not_applied", "note": "checked the mail log: nothing sent"})
    retried = world.owner.ok(f"/v1/contracts/{c['id']}/retry", version=world.get(c["id"])["version"])
    assert retried["status"] == "ready" and retried["attempt"] == 2
    ready = [e for e in world.events("cas.contract.ready.v1") if e["data"].get("previous_attempt") == 1][0]["data"]
    assert ready["effects_carried_over"] == ["upload-bundle"]
    assert ready["effects_to_rerun"] == ["notify-customers", "warm-cache"]

    lease2 = world.start(world.engineer, c["id"])
    again = step("upload-bundle", True, lease2)
    assert again["already_applied"] is True and again["attempt_no"] == 1  # recorded success is not repeated
    assert step("warm-cache", True, lease2)["already_applied"] is False
    assert step("notify-customers", False, lease2)["already_applied"] is False
    statuses = world.sql("SELECT step_key, attempt_no, status FROM external_effects WHERE tenant_id = %s ORDER BY created_at",
                         (world.tenant_id,))
    assert [(s["step_key"], s["attempt_no"], s["status"]) for s in statuses] == [
        ("upload-bundle", 1, "succeeded"), ("warm-cache", 1, "failed"), ("notify-customers", 1, "recovered"),
        ("warm-cache", 2, "authorized"), ("notify-customers", 2, "authorized")]


def test_t17_tenants_are_isolated(world: World):
    a = world
    up = a.contract("design")
    a.deliver(a.designer, up["id"], "design_spec", "tenant a spec")
    blob = a.owner.ok("/v1/blobs", {"content_base64": "c2VjcmV0"})
    b = World()
    b.goal()

    # Query: nothing of A is visible to B, whether listed or addressed by id.
    for path in (f"/v1/goals/{a.goal_id}", f"/v1/goals/{a.goal_id}/overview", f"/v1/goals/{a.goal_id}/events",
                 f"/v1/goals/{a.goal_id}/exceptions", f"/v1/contracts/{up['id']}", "/v1/artifacts/design_spec",
                 "/v1/artifacts/design_spec/versions/v1/content", f"/v1/agents/{a.designer.id}"):
        assert b.owner.get(path).status_code == 404, path
    assert b.owner.read("/v1/contracts")["items"] == []
    assert a.designer.id not in {p["id"] for p in b.owner.read("/v1/principals")["items"]}
    assert not {e["data"]["tenant_id"] for e in b.events()} - {b.tenant_id}
    event_id = a.events()[0]["id"]
    assert b.owner.get(f"/v1/events/{event_id}").status_code == 404
    assert b.admin.read("/v1/audit", limit=200)["items"] and all(
        row["tenant_id"] == b.tenant_id for row in b.admin.read("/v1/audit", limit=200)["items"])

    # Use: B cannot act on A's objects.
    assert b.designer.post(f"/v1/contracts/{up['id']}/claim", {"worker_id": "w"}).status_code == 404
    assert b.owner.post(f"/v1/goals/{a.goal_id}/activate", version=1).status_code == 404
    assert b.admin.post("/v1/grants", {"principal_id": a.designer.id, "role": "task_owner"}).status_code == 422

    # Reference: A's identities, goals, contracts, artifacts and blobs do not resolve inside B.
    base = {"goal_id": b.goal_id, "title": "x", "objective": "x", "requester_id": b.owner.id, "input_refs": [],
            "dependency_ids": [], "deliverable_spec": [{"type": "spec"}], "acceptance_checks": ["test:ok"],
            "permission_scope": [], "max_attempts": 1,
            "escalation_policy": {"on_stale": "hold", "on_conflict": "escalate", "decision_owner_id": b.owner.id}}
    assert error_code(b.owner.post("/v1/contracts", {**base, "goal_id": a.goal_id})) == "UNKNOWN_GOAL"
    assert error_code(b.owner.post("/v1/contracts", {**base, "owner_id": a.designer.id})) == "UNKNOWN_PRINCIPAL"
    assert error_code(b.owner.post("/v1/contracts", {**base, "dependency_ids": [up["id"]]})) == "UNKNOWN_CONTRACT"
    r = b.owner.post("/v1/decisions", {"goal_id": b.goal_id, "question": "q", "decision_owner_id": b.owner.id,
                                       "alternatives": [{"id": "a", "label": "a"}],
                                       "evidence_refs": [{"artifact_id": "design_spec", "version_id": "v1"}]})
    assert r.status_code == 422 and error_code(r) == "UNKNOWN_ARTIFACT"
    mine = b.owner.ok("/v1/contracts", {**base, "input_refs": [LATEST("design_spec")]})
    assert mine["status"] == "proposed" and b.get(mine["id"])["readiness"]["reason"] == "inputs_unresolved"
    c = b.contract("design")
    lease = b.start(b.designer, c["id"])
    r = b.designer.post(f"/v1/contracts/{c['id']}/artifacts",
                        {"lease_token": lease, "artifact_id": "theirs", "type": "spec", "uri": blob["uri"]})
    assert r.status_code == 422 and error_code(r) == "UNKNOWN_BLOB"
    # The same artifact id in two tenants names two unrelated artifacts.
    b.designer.ok(f"/v1/contracts/{c['id']}/artifacts",
                  {"lease_token": lease, "artifact_id": "design_spec", "type": "spec", "content_text": "tenant b spec"})
    assert a.owner.get("/v1/artifacts/design_spec/versions/v1/content").content == b"tenant a spec"
    assert b.owner.get("/v1/artifacts/design_spec/versions/v1/content").content == b"tenant b spec"


def test_t18_cancellation_fences_late_writes(world: World):
    c = world.contract("design")
    lease = world.start(world.designer, c["id"])
    world.designer.ok(f"/v1/contracts/{c['id']}/artifacts",
                      {"lease_token": lease, "artifact_id": "design_spec", "type": "spec", "content_text": "almost done"})
    r = world.engineer.post(f"/v1/contracts/{c['id']}/cancel", {"reason": "nope"}, version=world.get(c["id"])["version"])
    assert r.status_code == 403  # cancellation must be authorized
    cancelled = world.owner.ok(f"/v1/contracts/{c['id']}/cancel", {"reason": "direction changed"},
                               version=world.get(c["id"])["version"])
    assert cancelled["status"] == "cancelled" and cancelled["lease"] is None

    for path, body in ((f"/v1/contracts/{c['id']}/heartbeat", {"lease_token": lease}),
                       (f"/v1/contracts/{c['id']}/artifacts",
                        {"lease_token": lease, "artifact_id": "design_spec", "type": "spec", "content_text": "late"}),
                       (f"/v1/contracts/{c['id']}/submit", {"lease_token": lease}),
                       (f"/v1/contracts/{c['id']}/fail", {"lease_token": lease, "reason": "late"}),
                       (f"/v1/contracts/{c['id']}/effects", {"lease_token": lease, "step_key": "s", "scope": "staging:write"})):
        r = world.designer.post(path, body)
        assert r.status_code == 409 and error_code(r) == "LEASE_FENCED", path
    r = world.validator.post(f"/v1/contracts/{c['id']}/accept", {"check_results": [{"check": "test:ok", "status": "pass"}]},
                             version=cancelled["version"])
    assert r.status_code == 409 and error_code(r) == "INVALID_TRANSITION"
    r = world.owner.post(f"/v1/contracts/{c['id']}/retry", version=cancelled["version"])
    assert r.status_code == 409 and error_code(r) == "INVALID_TRANSITION"

    detail = world.get(c["id"])
    assert detail["status"] == "cancelled" and detail["version"] == cancelled["version"]
    assert [v["status"] for v in detail["artifacts"]] == ["rejected"]
    assert world.owner.read("/v1/artifacts/design_spec")["current_version_id"] is None
