# Coordinated Agent Systems (CAS) — Implementation Specification v0.1

Status: Proposed implementation baseline. Date: 2026-10-09. Scope: single-tenant pilot, multi-agent, human-supervised execution.

## 0. Decision summary

CAS is a coordination layer, not an agent runtime. It owns goal and work contracts, assignment, dependency readiness, artifact versions, validation, decisions, events, and policy enforcement. Agents own local planning/execution, using adapters. Start with a modular monolith (API + scheduler/reconciler + workers), PostgreSQL, object storage, and a transactional outbox. Do not start with a universal knowledge graph, free-form agent group chat, or a complex mission hierarchy.

Normative language: MUST indicates required v0.1 behavior; SHOULD recommended; MAY optional. A human approval is never equivalent to a tool authorization grant.

## 1. Goals and non-goals

Goals:
1. Multiple heterogeneous agents collaborate on a shared, revisable goal.
2. Each piece of work has an accountable owner, acceptance criteria, permissions, and versioned dependencies.
3. Agents can propose work and revisions; the coordination service verifies changes and controls authoritative state.
4. Stale inputs, conflicts, and failures are explicitly represented and recovered.
5. Human attention focuses on consequential decisions and exceptions.
6. A constrained and auditable governance layer provides goal-seeking without top-down certainty.

Non-goals: replacement for A2A/MCP, general truth engine, global organization hierarchy, runtime for model inference, production deployment automation, unrestricted agents choosing their own privileges, fully autonomous goal revision.

## 2. Primary domain objects

All objects MUST include `id`, `tenant_id`, `created_at`, `updated_at`, and `version` (monotonic integer). Foreign keys are tenant-scoped. Human and agent principals share one identity namespace with `kind` and issuer.

### GoalContract
- `id`, `title`, `objective`, `success_criteria[]` (explicit evaluable conditions), `constraints[]`, `preferences[]` (soft priorities), `owner_principal_id`, `status` (`draft|active|paused|completed|cancelled`), `version`.
- Goal revisions create a new version and an event; revisions to scope/success criteria trigger impact analysis of active contracts. Routine agent suggestions create *proposals*, not direct goal mutations.

### Principal and Capability
- `principal`: `id`, `kind` (`human|agent|service`), `display_name`, `issuer`, `external_subject`, `status`.
- `agent_capability`: `agent_id`, `capability_key`, `description`, `input_schema_ref`, `output_schema_ref`, `adapter_type`, `adapter_config_ref`, `limits`.
- Capability claims are hints; the scheduler may evaluate quality and availability but MUST NOT treat capability claims as permission grants.

### CoordinationContract
- `id`, `goal_id`, `parent_contract_id?`, `title`, `objective`, `requester_id`, `owner_id?`, `status`, `input_refs[]`, `deliverable_spec[]`, `acceptance_checks[]`, `dependency_ids[]`, `permission_scope[]`, `policy_version`, `priority`, `deadline?`, `lease?`, `attempt`, `max_attempts`, `escalation_policy`, `version`.
- `input_refs`: `{artifact_id, required_version, mode: exact|latest_accepted}`. `exact` pins a version; `latest_accepted` is resolved to a specific version when a contract becomes READY and remains pinned for an attempt.
- `deliverable_spec`: artifact type, JSON-schema ref where appropriate, acceptance predicate/check target.
- A contract is an *obligation to propose an acceptable deliverable*, not proof of completion or authorization to perform side effects.

### ArtifactVersion
- `artifact_id`, `version_id`, `contract_id`, `producer_id`, `uri` (immutable content-addressed or immutable-version object), `sha256`, `media_type`, `schema_ref?`, `input_lineage[]`, `created_at`, `status` (`proposed|accepted|rejected|superseded`).
- Artifact bytes MUST be immutable after publication. Artifacts are not automatically accepted when proposed.

### DecisionJunction
- `id`, `goal_id`, `contract_ids[]`, `question`, `kind` (`factual|preference|context|authority|other`), `alternatives[]`, `evidence_refs[]`, `decision_owner_id`, `state` (`open|awaiting_authority|resolved|superseded|cancelled`), `resolution?`, `version`.
- Decision resolution records actor, rationale, timestamp, current input versions, and impacted contract IDs. Decision does not waive permission or validation gates.

### Event
- CloudEvents 1.0 envelope `specversion`, `id`, `source`, `type`, `time`, `subject`, `datacontenttype`, `data`. CAS `data` includes `tenant_id`, `aggregate_id`, `aggregate_version`, `actor_id`, `correlation_id`, `causation_id`, `schema_version`, and typed payload. Source + ID is unique; aggregate version is monotonic. Events are durable once the underlying state mutation commits.

## 3. Contract state machine

States: `proposed`, `ready`, `leased`, `running`, `blocked`, `submitted`, `validating`, `accepted`, `rejected`, `failed`, `cancelled`.

Allowed transitions:
- proposed -> ready: contract valid, required inputs and dependencies satisfied, policy check passes.
- proposed -> blocked: dependencies missing or policy denies initiation (denials may require cancellation/escalation).
- ready -> leased: atomic claim of ownership, nonexpired lease acquired.
- leased -> running: owner acknowledges start with matching lease token.
- leased/running -> blocked: input invalidated, explicit hold, or unmet new dependency; current attempt fenced.
- running -> submitted: producer publishes artifact version(s) matching deliverable requirements.
- submitted -> validating: validation worker acquires validation job.
- validating -> accepted: all mandatory checks pass, current pinned input lineage remains valid, reviewer/authority conditions satisfied.
- validating -> rejected: validation failed; may create a new attempt if retry budget permits.
- leased/running -> failed: execution exception or exhausted retry policy.
- nonterminal -> cancelled: authorized cancellation and execution fence.
- blocked -> ready: explicit revalidation and dependency check after problem addressed.
- rejected/failed -> ready: explicit `retry` command increments attempt, refreshes input pins as policy permits.
- accepted is terminal for a contract version. Rework requires a replacement/successor contract or a new contract revision linked to the accepted version.

Transitions not listed MUST fail with `409 INVALID_TRANSITION`. Side-effecting workers MUST check lease fencing token and contract version before committing results. Leases expiring do not prove work stopped; tools must have idempotency and cancellation/compensation policies.

## 4. Dependency semantics

Represent `contract_dependency(upstream_id, downstream_id, requirement = accepted, created_at)`. Creation MUST reject cycles under a transactionally consistent graph check. A downstream contract cannot be READY until all upstream contracts are accepted and required artifact versions are resolved.

On upstream artifact supersession or goal revision:
1. Evaluate graph descendants affected by changed input references.
2. Emit `cas.input.invalidated.v1` for each affected active contract.
3. Fence active attempts before further CAS-managed publication; mark blocked.
4. Accepted descendants remain historical facts but are marked `requires_reassessment`; create successor work rather than mutating accepted output.
5. Re-run readiness after dependencies have been revalidated.

For exploratory work, new dependencies can be proposed dynamically. Changes to the dependency graph are CAS commands subject to concurrency, cycle checks, and policy.

## 5. Commands and APIs

REST JSON over HTTPS for v0.1. All mutating requests MUST carry `Idempotency-Key`, actor credentials, and where modifying an existing object `If-Match` with version ETag. Return `409` version or transition conflict, `403` policy denial, `422` schema/precondition violation, `429` quota, `503` transient dependency failure. Idempotency responses are retained for at least 24 hours within tenant and actor scope; same key with a different request fingerprint yields `409`.

Core routes:
- POST `/v1/goals`; GET `/v1/goals/{id}`; POST `/v1/goals/{id}/revisions`; POST `/v1/goals/{id}/activate`.
- GET `/v1/agents?capability=...`; POST `/v1/agents` (admin-controlled registration); GET `/v1/agents/{id}`.
- POST `/v1/contracts`; GET `/v1/contracts/{id}`; GET `/v1/contracts?goal_id=&status=`; POST `/v1/contracts/{id}/dependencies`; POST `/v1/contracts/{id}/revise`.
- POST `/v1/contracts/{id}/claim` (`worker_id`, `lease_seconds`); POST `/v1/contracts/{id}/heartbeat` (`lease_token`); POST `/v1/contracts/{id}/start`; POST `/v1/contracts/{id}/block`; POST `/v1/contracts/{id}/retry`; POST `/v1/contracts/{id}/cancel`.
- POST `/v1/contracts/{id}/artifacts` (register immutable artifact references); POST `/v1/contracts/{id}/submit`; POST `/v1/contracts/{id}/accept` (validator-authorized); POST `/v1/contracts/{id}/reject`.
- POST `/v1/decisions`; GET `/v1/decisions/{id}`; POST `/v1/decisions/{id}/resolve`.
- GET `/v1/goals/{id}/events?cursor=...`; GET `/v1/goals/{id}/overview`; GET `/v1/goals/{id}/exceptions`.

All command responses: `{resource, event_ids[], request_id}`. List APIs paginate with stable cursor and tenant filters. Event streams may be offered via SSE later, but polling is sufficient for the initial pilot.

### Example create contract request
```json
{
  "goal_id": "goal_01",
  "title": "Prototype concurrent AI editing",
  "objective": "Demonstrate conflict-safe shared editing in staging",
  "requester_id": "human_product_lead",
  "owner_id": "agent_engineer",
  "input_refs": [{"artifact_id":"spec_interaction","required_version":"v2","mode":"exact"}],
  "dependency_ids": ["contract_design"],
  "deliverable_spec": [{"type":"prototype","schema_ref":"schema://cas/prototype/v1"}],
  "acceptance_checks": ["test:no_lost_updates","review:integration"],
  "permission_scope": ["staging:read","staging:write"],
  "escalation_policy": {"on_stale":"hold","on_conflict":"reconcile_then_escalate","decision_owner_id":"human_product_lead"},
  "max_attempts": 3
}
```

### Example event
```json
{
  "specversion":"1.0",
  "id":"evt_002",
  "source":"urn:cas:coordination",
  "type":"cas.input.invalidated.v1",
  "subject":"contracts/contract_engineer",
  "time":"2026-10-09T13:00:00Z",
  "datacontenttype":"application/json",
  "data": {
    "tenant_id":"tenant_demo", "aggregate_id":"contract_engineer", "aggregate_version":8,
    "actor_id":"service_reconciler", "correlation_id":"corr_01", "causation_id":"evt_001",
    "schema_version":1, "input_artifact_id":"spec_interaction", "old_version":"v2", "new_version":"v3", "action":"hold_and_revalidate"
  }
}
```

## 6. Assignment and execution

Scheduler checks dependency readiness, contract capability requirements, agent availability, concurrency limits, and permissions. A ready job is claimed atomically with an expiring lease and monotonically increasing fencing token. Job assignment is not authorization for tool side effects.

Adapter contract:
- `capabilities(): CapabilityDescriptor[]`
- `start(contract, pinned_inputs, lease_token, policy_context) -> external_run_id`
- `poll(external_run_id) -> progress | submitted_artifacts | error`
- `cancel(external_run_id) -> acknowledgement` (best effort)
- `collect(external_run_id) -> artifact_refs[]`

For an A2A adapter map CAS contract to A2A task/context and map returned artifacts/status updates back to CAS events. For a local process adapter use a subprocess/job queue with sandbox and bounded credentials. For tool access, use MCP where appropriate but enforce separate CAS policy at the tool proxy/action boundary.

## 7. Conflict and decision resolution

Classify conflicts as: `version_collision`, `dependency_invalid`, `artifact_incompatibility`, `evidence_disagreement`, `priority_tradeoff`, `permission_denied`.

Automatic resolution is only allowed when a preapproved deterministic policy applies (e.g., identical content hash, schema-compatible nonoverlapping patch, rerun on latest inputs). Never use majority agent vote as evidence verification. Limit autonomous negotiation to two rounds per conflict in the pilot; require a changed artifact/evidence reference to consume a new round. Otherwise open a DecisionJunction with alternatives, evidence, consequences, owner and held dependents. Permission denial cannot be resolved by agent negotiation.

Decision resolution must revalidate referenced object versions; if stale, return `409 DECISION_BASELINE_CHANGED`. Once resolved, trigger dependency readiness/reconciliation. Human decision grants no tool permissions unless a separate explicit authorization flow does so.

## 8. Policy model

Two independent layers:
- Goal preferences: soft guidance (outcomes, weights, exploration budget, tradeoffs), versioned and human-owned.
- Authority policies: enforced permission checks at task claim, artifact acceptance, sensitive read/write and external side-effect boundaries.

Pilot RBAC: `goal_owner`, `task_requester`, `task_owner`, `validator`, `decision_owner`, `admin`, `auditor`. Resource-level scopes narrow roles. Default deny when a permission is absent. Authority delegation is intersection of delegator grant, agent service grant, task envelope and resource policy, never union. No agent may edit its own grants or approve its own privileged exception. Requests to change goals create proposals subject to goal_owner review.

Attention policy: `ambient`, `digest`, `decision`, `interrupt`. Escalate failed security/policy boundaries to mandated recipients even if user prefers few notifications. Digest summarization cannot mark an unresolved exception resolved. Record why an escalation decision was made.

## 9. Persistence and consistency

PostgreSQL recommended tables: `principals`, `agent_capabilities`, `goals`, `goal_revisions`, `contracts`, `contract_revisions`, `contract_dependencies`, `contract_attempts`, `leases`, `artifacts`, `artifact_versions`, `decisions`, `decision_resolutions`, `policy_grants`, `events`, `outbox`, `inbox_dedup`, `idempotency_records`, `audit_actions`.

- Local transactions atomically update aggregate state, version and outbox event.
- Use `UPDATE ... WHERE version = expected_version` for optimistic concurrency.
- `SELECT ... FOR UPDATE SKIP LOCKED` (or queue equivalent) for scheduler claims.
- Deliver outbox at least once; consumers dedupe by `(source,id)` and apply monotonic aggregate versions.
- Exactly-once external side effects are not assumed; use idempotency and compensating actions where feasible.
- Object storage is immutable by artifact version and hash; verify hash on ingestion.
- Apply per-tenant isolation to queries, artifacts, events, and adapter credentials.

## 10. Scheduler/reconciler pseudocode

```python
for contract in contracts_with_changed_readiness():
    with db.transaction():
        c = lock(contract.id)
        if c.status in TERMINAL: continue
        if has_invalidated_pins(c):
            fence_attempt(c)
            transition(c, "blocked", reason="stale_input")
        elif all_dependencies_accepted(c) and policy_can_start(c):
            if c.status in ("proposed", "blocked"):
                pin_latest_inputs(c)
                transition(c, "ready")

for c in claimable_ready_contracts():
    with db.transaction():
        c = lock(c.id)
        if c.status != "ready": continue
        agent = choose_capable_authorized_agent(c)
        if agent is None: continue
        lease = issue_fenced_lease(c, agent)
        transition(c, "leased", owner=agent.id, lease=lease)
```

## 11. MVP user interface

A single goal workspace:
1. Goal strip: objective, constraints, status, version.
2. Contract list: owner, dependencies, lease/execution status, artifacts, validation.
3. Exceptions/decisions queue: blocked, invalidated, permission denied, unresolved conflict.
4. Change inspector: before/after state, event provenance, affected work.
5. Explicit actions: propose contract, accept/reject, retry, revise goal, resolve decision; authorization failures visible.

Do not make an agent-avatar wall, generic activity firehose or mission-alignment score the default view.

## 12. Conformance scenarios (must automate)

T01 — Create goal and two dependent contracts; downstream remains proposed/blocked until upstream accepted.
T02 — Accept upstream with artifact v1; downstream becomes ready and pins v1.
T03 — Upstream superseded v2 while downstream running; running attempt fenced, contract blocked, no stale artifact accepted.
T04 — Two claimers race; only one acquires active fenced lease.
T05 — Lease expires, new lease issued; late worker submission with old token rejected.
T06 — Duplicate submit with same idempotency key returns same result; same key different body fails.
T07 — Concurrent update with outdated `If-Match` returns 409 and does not overwrite data.
T08 — New dependency would introduce cycle; reject without mutating graph.
T09 — Missing permission for external side effect; deny even if goal says it is useful.
T10 — Agent proposes a revised goal; owner approval required before activation.
T11 — Validator rejects artifact; retry increments attempt and preserves failed artifact/version and audit record.
T12 — Two conflicting proposals exceed permitted reconciliation rounds; create open DecisionJunction and hold dependent work.
T13 — Decision resolved against stale referenced artifact version; reject with baseline conflict.
T14 — Validator cannot accept a contract whose inputs became invalid during validation.
T15 — Outbox redelivers event; consumer state remains exactly once logically via dedup and version checks.
T16 — Partial external failure; record successful effects, retry only idempotent steps and flag manual recovery otherwise.
T17 — Tenant A cannot query, use, or reference Tenant B's artifacts, identities or events.
T18 — Cancellation fences future CAS writes; late worker cannot transition the contract or publish accepted artifacts.

## 13. Operational service levels and limits (initial targets, to validate)

- Up to 20 concurrent agents and 1,000 contracts per tenant pilot.
- Coordination write API p95 < 300 ms, excluding downstream agent execution; readiness processing p95 < 5 seconds.
- No lost committed state/event after process crash (database durability assumed).
- Event delivery at least once within 10 seconds under normal operation; monitors for outbox lag and dead-letter count.
- Lease default 60 seconds; heartbeat every 20 seconds; durations configurable, never assume expiry stopped external tool activity.
- Retry budget default 3 attempts with exponential backoff and jitter.
- Metrics: goal acceptance rate, accepted contracts per unit cost, stale-result rejection rate, human escalations per completed goal, failure recovery time, policy denials, queue lag, validation failures and external-action duplication.
- Audit retention/PII rules must be set before real customer data enters the system.

## 14. Implementation plan and milestones

**M0 — State and contract core:** tables, goal/contracts CRUD, version-aware mutation, authenticated principals, outbox, state machine unit tests.

**M1 — Agent execution:** adapter interface, local agent adapter, scheduler/lease/fencing, artifact registration, validator, dependency gates.

**M2 — Reconciliation:** graph versioning, invalidation propagation, conflict objects, bounded automatic retries, human decision endpoint.

**M3 — Oversight and governance:** goal workspace UI, events/exception views, role policy and action gateway, attention rules, audit.

**M4 — Pilot and hardening:** A2A adapter (if required), MCP gateway integration (if required), full T01–T18 conformance suite, fault injection, load tests, security review.

Suggested repository modules:
```
cas/
  api/              # HTTP routes, authentication, request schemas
  domain/           # entities, transitions, invariants, policy types
  service/          # commands, contract lifecycle, reconciler
  adapters/         # local agent, A2A, tool gateway
  persistence/      # SQL models, migrations, outbox
  workers/          # scheduler, validation, notification
  ui/               # goal overview, contracts, exceptions
  tests/            # conformance, races, fault injection
  docs/             # schemas, ADRs, threat model
```

## 15. Open questions and default decisions

- Single-tenant pilot now; design tenant-scoped IDs and isolation from day one.
- One active owner per contract version; group work split into subcontracts.
- PostgreSQL authoritative; Redis optional cache, never system of record.
- HTTP polling first; SSE can be added without changing domain events.
- Stable goal owner has authority to revise goals; agent can propose revision.
- Validation is separate from agent self-report; human signoff only when specified.
- Contracts pin input versions on readiness; changed inputs fence affected attempts.
- No arbitrary autonomous policy mutation.
- Tool transport and model vendor remain pluggable.

## 16. External standards touchpoints

A2A official spec: https://a2a-protocol.org/v0.3.0/specification/ (agent discovery, communication, tasks, artifacts).
MCP authorization specification: https://github.com/modelcontextprotocol/modelcontextprotocol/blob/main/docs/specification/2025-11-25/basic/authorization.mdx (transport authorization considerations).
CloudEvents spec: https://github.com/cloudevents/spec/blob/main/cloudevents/spec.md (standard event envelope).

**Version 0.1 implementation boundary:** a successful reference implementation can coordinate three distinct agents to complete a version-dependent goal, reject a stale result, resolve a conflict with human input, block an unauthorized side effect, and recover from worker failure without silently corrupting accepted state.
