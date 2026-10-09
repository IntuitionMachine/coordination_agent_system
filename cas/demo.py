"""`cas demo`: the spec's v0.1 implementation boundary, run end to end against the database.

Three distinct agents complete a version-dependent goal. Along the way CAS rejects a stale
result, a human resolves a conflict, an unauthorized side effect is blocked, and a worker
failure is recovered. A few items are left open on purpose so the workspace has work to show."""
from __future__ import annotations

import secrets
import time

from cas.config import settings
from cas.domain.errors import CasError, PolicyDenied
from cas.persistence.db import transaction
from cas.service import artifacts, contracts, decisions, effects, goals, notifications, principals
from cas.service.core import Ctx, new_id
from cas.workers import reconciler

SCOPES = ["staging:read", "staging:write"]


def run_demo(tenant_id: str | None = None) -> dict:
    tenant_id = tenant_id or f"demo_{secrets.token_hex(3)}"
    step = iter(range(1, 100))

    def say(text: str) -> None:
        print(f"{next(step):>2}. {text}")

    def do(actor: dict, fn):
        with transaction() as tx:
            return fn(Ctx(tx=tx, tenant_id=tenant_id, actor=actor, request_id=new_id("demo")))

    def refused(actor: dict, fn) -> CasError:
        try:
            do(actor, fn)
        except PolicyDenied as err:
            do(actor, lambda ctx: decisions.record_denial(ctx, err))  # what the API layer does on a 403
            return err
        except CasError as err:
            return err
        raise AssertionError("expected CAS to refuse this")

    with transaction() as tx:
        boot = principals.bootstrap_tenant(tx, tenant_id, "CAS demo", "Avery (admin)")
    admin = boot["admin"]
    credentials = {"Avery (admin)": boot["token"]}

    def human(name: str, roles: list[str], scopes: list[str] | None = None) -> dict:
        p = do(admin, lambda ctx: principals.create_principal(ctx, {
            "kind": "human", "display_name": name, "external_subject": name.lower().replace(" ", ".")}))
        credentials[name] = p.pop("token")
        for role in roles:
            do(admin, lambda ctx: principals.create_grant(ctx, {"principal_id": p["id"], "role": role, "scopes": scopes or []}))
        return p

    def agent(name: str, capability: str) -> dict:
        a = do(admin, lambda ctx: principals.register_agent(ctx, {
            "display_name": name, "external_subject": name.lower().replace(" ", "-"),
            "capabilities": [{"capability_key": capability, "description": f"Produces {capability} artifacts"}]}))
        a.pop("token")
        do(admin, lambda ctx: principals.create_grant(ctx, {"principal_id": a["id"], "role": "task_owner", "scopes": SCOPES}))
        return a

    priya = human("Priya (product lead)", ["goal_owner", "task_requester"], SCOPES)
    sam = human("Sam (reviewer)", ["validator"])
    designer, engineer, analyst = agent("Design agent", "spec"), agent("Engineering agent", "prototype"), agent("Analysis agent", "report")

    goal = do(priya, lambda ctx: goals.create(ctx, {
        "title": "Conflict-safe shared editing",
        "objective": "Demonstrate conflict-safe shared editing with AI assistance in staging",
        "success_criteria": ["No lost updates under concurrent edits", "Integration review signed off"],
        "constraints": ["Staging environment only"],
        "preferences": ["Prefer small reversible steps", "Shipping to production early would be useful"],
        "owner_principal_id": priya["id"]}))
    gid = goal["id"]
    do(admin, lambda ctx: principals.set_resource_policy(ctx, gid, SCOPES))
    do(priya, lambda ctx: goals.set_status(ctx, gid, "active", None))
    do(priya, lambda ctx: artifacts.register_goal_input(ctx, gid, {
        "artifact_id": "interaction_spec", "type": "spec", "media_type": "text/markdown",
        "content_text": "# Interaction spec v1\nEdits merge per paragraph."}))
    say(f"Tenant '{tenant_id}': goal active, three agents registered, interaction_spec v1 published.")

    def contract(title: str, deliverable: str, checks: list[str], *, deps=(), inputs=(), scope=(), owner=None, on_stale="hold") -> dict:
        return do(priya, lambda ctx: contracts.create(ctx, {
            "goal_id": gid, "title": title, "objective": f"Deliver the {title.lower()}", "requester_id": priya["id"],
            **({"owner_id": owner["id"]} if owner else {}),
            "input_refs": [{"artifact_id": a, "required_version": "latest", "mode": "latest_accepted"} for a in inputs],
            "dependency_ids": list(deps), "deliverable_spec": [{"type": deliverable}], "acceptance_checks": checks,
            "permission_scope": list(scope), "max_attempts": 3,
            "escalation_policy": {"on_stale": on_stale, "on_conflict": "reconcile_then_escalate", "decision_owner_id": priya["id"]}}))

    design = contract("Merge design", "spec", ["review:design"], inputs=["interaction_spec"], owner=designer)
    build = contract("Editing prototype", "prototype", ["test:no_lost_updates", "review:integration"],
                     deps=[design["id"]], inputs=["design_doc"], scope=["staging:write"], owner=engineer)
    report = contract("Staging results report", "report", ["nonempty:", "review:findings"],
                      deps=[build["id"]], inputs=["prototype_build"], owner=analyst)

    def work(actor: dict, cid: str) -> str:
        _, lease = do(actor, lambda ctx: contracts.claim(ctx, cid, f"{actor['display_name']} worker", 60))
        do(actor, lambda ctx: contracts.start(ctx, cid, lease))
        return lease

    def publish(actor: dict, cid: str, lease: str, artifact_id: str, kind: str, text: str) -> str:
        return do(actor, lambda ctx: contracts.register_artifact(ctx, cid, lease, {
            "artifact_id": artifact_id, "type": kind, "media_type": "text/markdown", "content_text": text}))[1]["version_id"]

    def accept(cid: str) -> dict:
        c = do(sam, lambda ctx: contracts.get(ctx.tx, tenant_id, cid))
        return do(sam, lambda ctx: contracts.accept(ctx, cid, [{"check": k, "status": "pass"} for k in c["acceptance_checks"]], None))

    lease = work(designer, design["id"])
    publish(designer, design["id"], lease, "design_doc", "spec", "# Merge design v1\nParagraph-level last-writer-wins.")
    do(designer, lambda ctx: contracts.submit(ctx, design["id"], lease))
    accept(design["id"])
    say("Design agent delivered design_doc v1; Sam accepted it. The prototype contract became ready, pinned to v1.")

    build_lease = work(engineer, build["id"])
    err = refused(engineer, lambda ctx: effects.authorize(ctx, build["id"], build_lease, {
        "step_key": "deploy-production", "scope": "prod:deploy", "idempotent": False}))
    say(f"Engineering agent asked to deploy to production: refused ({err.code}), audited, and escalated to Priya. "
        "The goal's preference for shipping early granted nothing.")

    with transaction() as tx:
        tx.execute("UPDATE leases SET expires_at = now() - interval '1 second' WHERE tenant_id = %s AND contract_id = %s AND status = 'active'",
                   (tenant_id, build["id"]))
    reconciler.run_once(tenant_id)
    err = refused(engineer, lambda ctx: contracts.heartbeat(ctx, build["id"], build_lease, None))
    for _ in range(100):  # the automatic retry waits out its backoff
        if do(admin, lambda ctx: contracts.get(ctx.tx, tenant_id, build["id"]))["status"] == "ready":
            break
        time.sleep(0.2)
        reconciler.run_once(tenant_id)
    build_lease = work(engineer, build["id"])
    say(f"The engineering worker went silent and its lease expired. The reconciler failed the attempt and opened attempt 2; "
        f"the old worker's heartbeat was refused ({err.code}).")

    do(priya, lambda ctx: artifacts.register_goal_input(ctx, gid, {
        "artifact_id": "interaction_spec", "type": "spec", "media_type": "text/markdown",
        "content_text": "# Interaction spec v2\nEdits merge per sentence, with presence cursors."}))
    successor = do(priya, lambda ctx: contracts.revise(ctx, design["id"], {"rationale": "interaction spec changed to v2"}, None))
    lease = work(designer, successor["id"])
    publish(designer, successor["id"], lease, "design_doc", "spec", "# Merge design v2\nSentence-level CRDT merge.")
    do(designer, lambda ctx: contracts.submit(ctx, successor["id"], lease))
    accept(successor["id"])
    err = refused(engineer, lambda ctx: contracts.register_artifact(ctx, build["id"], build_lease, {
        "artifact_id": "prototype_build", "type": "prototype", "content_text": "prototype built against design v1"}))
    say(f"The spec moved to v2, so a successor contract produced design_doc v2. The running prototype attempt was fenced "
        f"and its stale result refused ({err.code}).")

    do(priya, lambda ctx: contracts.revalidate(ctx, build["id"], None))
    build_lease = work(engineer, build["id"])
    effect = do(engineer, lambda ctx: effects.authorize(ctx, build["id"], build_lease, {
        "step_key": "deploy-staging", "scope": "staging:write", "idempotent": True}))
    do(engineer, lambda ctx: effects.report(ctx, build["id"], effect["id"], build_lease, True, {"url": "https://staging.example.invalid/proto"}))
    publish(engineer, build["id"], build_lease, "prototype_build", "prototype", "prototype built against design v2")
    do(engineer, lambda ctx: contracts.submit(ctx, build["id"], build_lease))
    accept(build["id"])
    say("Priya revalidated the prototype contract against v2. The engineering agent deployed to staging (authorized), "
        "delivered, and Sam accepted.")

    plan_a = contract("Rollout plan: gradual", "spec", ["review:plan"], owner=designer)
    plan_b = contract("Rollout plan: all at once", "spec", ["review:plan"], owner=analyst)
    rollout = contract("Staging rollout", "report", ["review:rollout"], deps=[plan_a["id"]])
    la, lb = work(designer, plan_a["id"]), work(analyst, plan_b["id"])
    va = publish(designer, plan_a["id"], la, "rollout_gradual", "spec", "10% of documents first.")
    vb = publish(analyst, plan_b["id"], lb, "rollout_full", "spec", "All documents at once.")
    refs = lambda: [{"artifact_id": "rollout_gradual", "version_id": va}, {"artifact_id": "rollout_full", "version_id": vb}]  # noqa: E731
    conflict = do(designer, lambda ctx: decisions.open_conflict(ctx, {
        "goal_id": gid, "kind": "priority_tradeoff", "contract_ids": [plan_a["id"], plan_b["id"]], "proposals": refs(),
        "description": "Gradual and all-at-once rollout plans cannot both be followed"}))
    va = publish(designer, plan_a["id"], la, "rollout_gradual", "spec", "10% first, then 50% after a day.")
    do(designer, lambda ctx: decisions.add_round(ctx, conflict["id"], {"proposals": refs()}, None))
    vb = publish(analyst, plan_b["id"], lb, "rollout_full", "spec", "All at once, behind a kill switch.")
    do(analyst, lambda ctx: decisions.add_round(ctx, conflict["id"], {"proposals": refs()}, None))
    va = publish(designer, plan_a["id"], la, "rollout_gradual", "spec", "5% first, then 50% after a day.")
    conflict = do(designer, lambda ctx: decisions.add_round(ctx, conflict["id"], {"proposals": refs()}, None))
    say("Two agents proposed incompatible rollout plans. After two reconciliation rounds without agreement CAS opened a "
        "decision for Priya and held the dependent rollout contract.")
    do(priya, lambda ctx: decisions.resolve(ctx, conflict["decision_id"], {
        "chosen_alternative": f"rollout_gradual@{va}", "rationale": "A gradual rollout limits the blast radius."}, None))
    do(designer, lambda ctx: contracts.submit(ctx, plan_a["id"], la))
    accept(plan_a["id"])
    do(priya, lambda ctx: contracts.cancel(ctx, plan_b["id"], "The gradual plan was chosen", None))
    say("Priya chose the gradual plan. It was accepted, the other was cancelled, and the rollout contract became ready.")

    lease = work(analyst, report["id"])
    publish(analyst, report["id"], lease, "staging_report", "report", "# Staging results\n0 lost updates in 2,000 concurrent edits.")
    do(analyst, lambda ctx: contracts.submit(ctx, report["id"], lease))
    do(analyst, lambda ctx: goals.revise(ctx, gid, {
        "changes": {"success_criteria": ["No lost updates under concurrent edits", "Integration review signed off",
                                         "p95 merge latency under 200 ms"]},
        "rationale": "Staging runs show merge latency is the main risk to users"}, None))
    say("The analysis agent submitted its report (waiting for validation) and proposed a goal revision (waiting for Priya).")

    notifications.dispatch_pending(limit=10_000)
    with transaction() as tx:
        counts = {r["status"]: r["n"] for r in tx.all(
            "SELECT status, count(*) AS n FROM contracts WHERE tenant_id = %s GROUP BY status", (tenant_id,))}
    print("\nContracts:", ", ".join(f"{n} {s}" for s, n in sorted(counts.items())))
    print("Left open for you in the workspace: the report's validation, the permission-denied decision, "
          "the ready rollout contract and the proposed goal revision.\n")
    print("Sign in to the workspace (cas serve, then http://127.0.0.1:8000) with one of these credentials:")
    for name, credential in credentials.items():
        print(f"  {name:<22} {credential}")
    print("\nPriya owns the goal, Sam validates, Avery administers grants.")
    saved = settings.data_dir / f"demo-credentials-{tenant_id}.txt"
    saved.parent.mkdir(parents=True, exist_ok=True)
    saved.write_text("".join(f"{name}\t{credential}\n" for name, credential in credentials.items()))
    saved.chmod(0o600)
    print(f"They are not shown again, but a copy is in {saved} (demo use only).")
    return {"tenant_id": tenant_id, "goal_id": gid, "credentials": credentials}
