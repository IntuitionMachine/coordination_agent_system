"""The scheduler, adapters, validation worker and attention policy (spec §6, §8, §10)."""
from __future__ import annotations

import sys
import time

from cas.adapters.local import InProcessAdapter, LocalProcessAdapter
from cas.service import notifications
from cas.tests.conftest import World
from cas.workers import validation
from cas.workers.runner import tick
from cas.workers.scheduler import Scheduler

AUTO_CHECKS = ["nonempty:", "hash:verify"]


def run_until(world: World, scheduler: Scheduler, contract_id: str, status: str, seconds: float = 10.0) -> dict:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        tick(scheduler, world.tenant_id)
        detail = world.get(contract_id)
        if detail["status"] == status:
            return detail
        time.sleep(0.05)
    raise AssertionError(f"contract is '{world.get(contract_id)['status']}', expected '{status}'")


def test_scheduler_assigns_runs_and_validates_through_an_in_process_adapter(world: World):
    adapter = InProcessAdapter()
    adapter.register("write_report", lambda request: [
        {"artifact_id": "report", "type": "report", "media_type": "text/plain",
         "content_text": "report based on " + request.pinned_inputs[0]["content_text"]}])
    scheduler = Scheduler({"in_process": adapter}, {"writer": {"function": "write_report"}})
    writer = world.agent("agent-writer", ["report"], adapter_type="in_process", adapter_config_ref="writer")
    world.agent("agent-unauthorized", ["report"], scopes=[], adapter_type="in_process", adapter_config_ref="writer")

    world.owner.ok(f"/v1/goals/{world.goal_id}/artifacts", {"artifact_id": "brief", "type": "spec", "content_text": "the brief"})
    c = world.contract("report", inputs=[{"artifact_id": "brief", "required_version": "v1", "mode": "exact"}],
                       deliverable="report", checks=AUTO_CHECKS, scope=["staging:read"])
    assert c["status"] == "ready"

    detail = run_until(world, scheduler, c["id"], "accepted")
    # Capability is a hint; authority decided the assignment: the agent without the scope was never chosen.
    assert detail["owner_id"] == writer.id
    assert detail["checks"] == {"nonempty:": "pass", "hash:verify": "pass"}
    assert world.owner.get("/v1/artifacts/report/versions/v1/content").content == b"report based on the brief"
    assert detail["artifacts"][0]["input_lineage"][0]["version_id"] == "v1"


def test_worker_failure_is_recovered_by_automatic_retry(world: World):
    calls = []

    def flaky(request):
        calls.append(request.contract["attempt"])
        if len(calls) == 1:
            raise RuntimeError("model endpoint timed out")
        return [{"artifact_id": "report", "type": "report", "content_text": "second attempt worked"}]

    adapter = InProcessAdapter()
    adapter.register("flaky", flaky)
    scheduler = Scheduler({"in_process": adapter}, {"flaky": {"function": "flaky"}})
    world.agent("agent-flaky", ["report"], adapter_type="in_process", adapter_config_ref="flaky")
    c = world.contract("report", deliverable="report", checks=AUTO_CHECKS)

    detail = run_until(world, scheduler, c["id"], "accepted")
    assert calls == [1, 2] and detail["attempt"] == 2
    assert [(a["attempt_no"], a["status"]) for a in detail["attempts"]] == [(1, "failed"), (2, "accepted")]


def test_local_process_adapter_runs_a_command_without_cas_credentials(world: World, tmp_path):
    script = tmp_path / "agent.py"
    script.write_text(
        "import json, os\n"
        "task = json.load(open(os.environ['CAS_TASK_FILE']))\n"
        "leaked = [k for k in os.environ if 'TOKEN' in k or 'LEASE' in k or 'AUTH' in k]\n"
        "out = {'title': task['contract']['title'], 'leaked': leaked, 'has_lease': 'lease_token' in json.dumps(task)}\n"
        "json.dump([{'artifact_id': 'proto', 'type': 'prototype', 'content_json': out}],\n"
        "          open(os.path.join(os.environ['CAS_OUTPUT_DIR'], 'artifacts.json'), 'w'))\n")
    scheduler = Scheduler({"local_process": LocalProcessAdapter()},
                          {"builder": {"command": [sys.executable, str(script)], "timeout_seconds": 30}})
    world.agent("agent-builder", ["prototype"], adapter_type="local_process", adapter_config_ref="builder")
    c = world.contract("prototype", deliverable="prototype", checks=["json:prototype"])

    run_until(world, scheduler, c["id"], "accepted")
    produced = world.owner.get("/v1/artifacts/proto/versions/v1/content").json()
    assert produced == {"title": "prototype", "leaked": [], "has_lease": False}


def test_review_checks_wait_for_a_human_and_cannot_be_signed_by_a_machine(world: World):
    c = world.contract("design", checks=["nonempty:", "review:design"])
    world.produce(world.designer, c["id"], "design_spec", "the spec")
    assert validation.run_once(world.tenant_id) == {"waiting_for_human": 1}
    detail = world.get(c["id"])
    assert detail["status"] == "validating" and detail["checks"] == {"nonempty:": "pass", "review:design": None}

    # The producer cannot validate its own work, and an incomplete check set cannot be accepted.
    world.admin.ok("/v1/grants", {"principal_id": world.designer.id, "role": "validator"})
    r = world.designer.post(f"/v1/contracts/{c['id']}/accept", {"check_results": [{"check": "review:design", "status": "pass"}]},
                            version=detail["version"])
    assert r.status_code == 403
    world.admin.ok("/v1/grants", {"principal_id": world.engineer.id, "role": "validator"})
    r = world.engineer.post(f"/v1/contracts/{c['id']}/accept", {"check_results": [{"check": "review:design", "status": "pass"}]},
                            version=detail["version"])
    assert r.status_code == 403  # an agent validator cannot sign a human review
    r = world.validator.post(f"/v1/contracts/{c['id']}/accept", {}, version=detail["version"])
    assert r.status_code == 422 and r.json()["error"]["details"]["pending"] == ["review:design"]

    accepted = world.validator.ok(f"/v1/contracts/{c['id']}/accept",
                                  {"check_results": [{"check": "review:design", "status": "pass"}]}, version=detail["version"])
    assert accepted["status"] == "accepted"


def test_attention_policy_mandates_policy_failures_and_digests_resolve_nothing():
    world = World()
    world.goal()
    world.owner.ok("/v1/me/attention", {"level": "digest"})
    c = world.contract("build", deliverable="prototype", scope=["staging:write"])
    lease = world.start(world.engineer, c["id"])
    assert world.engineer.post(f"/v1/contracts/{c['id']}/effects",
                               {"lease_token": lease, "step_key": "deploy", "scope": "prod:deploy"}).status_code == 403
    world.owner.ok(f"/v1/contracts/{c['id']}/block", {"reason": "pausing"}, version=world.get(c["id"])["version"])
    notifications.dispatch_pending(limit=10_000)

    notes = world.owner.read("/v1/notifications", limit=200)["items"]
    denial = next(n for n in notes if n["event_type"] == "cas.policy.denied.v1")
    blocked = next(n for n in notes if n["event_type"] == "cas.contract.blocked.v1")
    # A policy boundary failure interrupts even someone who asked for digests only, and says why.
    assert denial["level"] == "interrupt" and denial["mandated"] and "despite" in denial["reason"]
    # An ordinary exception respects the preference, and records that it was lowered.
    assert blocked["level"] == "digest" and not blocked["mandated"] and "lowered" in blocked["reason"]

    before = world.owner.read(f"/v1/goals/{world.goal_id}/exceptions")["items"]
    assert {i["kind"] for i in before} >= {"blocked", "permission_denied"}
    digest = world.owner.read("/v1/notifications/digest")
    assert digest["open_exceptions"][world.goal_id] == len(before)
    world.owner.ok("/v1/notifications/read", {"ids": [n["id"] for n in notes]})
    # Reading or summarising notifications changes nothing about what is unresolved.
    assert world.owner.read(f"/v1/goals/{world.goal_id}/exceptions")["items"] == before


def test_goal_revision_impact_analysis_holds_named_work_and_flags_the_rest(world: World):
    a = world.contract("design")
    b = world.contract("research", deliverable="report")
    lease = world.start(world.designer, a["id"])
    goal = world.owner.read(f"/v1/goals/{world.goal_id}")
    revised = world.owner.ok(f"/v1/goals/{world.goal_id}/revisions",
                             {"changes": {"objective": "Ship conflict-safe editing for mobile first"},
                              "rationale": "mobile is the priority now", "hold_contract_ids": [a["id"]]},
                             version=goal["version"])
    assert revised["version"] == goal["version"] + 1 and revised["impact"]["held_contract_ids"] == [a["id"]]
    held = world.get(a["id"])
    assert held["status"] == "blocked" and held["blocked_reason"] == "goal_revised"
    assert world.designer.post(f"/v1/contracts/{a['id']}/submit", {"lease_token": lease}).status_code == 409
    assert world.get(b["id"])["requires_reassessment"] is True and world.get(b["id"])["status"] == "ready"
    change = world.events("cas.goal.revised.v1")[0]["data"]
    assert change["before"] == {"objective": "Ship conflict-safe shared editing"} and change["scope_changed"] is True
