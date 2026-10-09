# Coordinated Agent Systems (CAS)

Reference implementation of `CAS_Implementation_Package_v0.1/CAS_IMPLEMENTATION_SPEC_v0.1.md`: a coordination
layer for several agents working on one shared goal under human supervision. CAS owns goals, work contracts,
dependencies, leases, artifact versions, validation, decisions, events and policy. Agents do their own work and
talk to CAS through the HTTP API or an adapter.

## Run it

```sh
uv venv --python 3.12 && uv pip install --python .venv/bin/python -e ".[dev]"
.venv/bin/cas demo            # creates a demo tenant, runs the three-agent scenario, prints sign-in credentials
.venv/bin/cas serve           # API + workspace UI + worker loop on http://127.0.0.1:8000
.venv/bin/python -m pytest    # state machine, T01-T18 conformance, workers and policy
```

With no `CAS_DATABASE_URL`, CAS starts an embedded PostgreSQL 16 under `.cas-data/` (development only). For a
real server, `docker compose up -d` and set `CAS_DATABASE_URL`. Other commands: `cas bootstrap --tenant <id>`
(new tenant and first admin), `cas worker` (workers as their own process; use `cas serve --no-worker` with it),
`cas migrate`. Settings are environment variables listed in `cas/config.py`.

Every request carries `Authorization: Bearer <credential>`; the tenant comes from the credential. Mutating
requests need `Idempotency-Key`, and commands by people on existing objects need `If-Match: "<version>"`.

## Layout

| Path | Contents |
| --- | --- |
| `cas/domain` | state machines, error types, JSON Schema validation |
| `cas/service` | commands: goals, contracts, artifacts, decisions and conflicts, side-effect gateway, policy, read models |
| `cas/persistence` | PostgreSQL access, the migration, content-addressed blob store |
| `cas/workers` | reconciler, scheduler, validation, worker loop |
| `cas/adapters` | adapter contract, local subprocess adapter, in-process adapter |
| `cas/api` | HTTP routes, authentication, idempotency |
| `cas/ui` | the goal workspace (static HTML, CSS, JS) |
| `cas/tests` | conformance and unit tests |

## Decisions taken where the spec left room

- **`ready -> blocked` is allowed.** The spec's transition table omits it, but its reconciler pseudocode blocks any
  non-terminal contract with invalidated pins. Every other unlisted transition returns `409 INVALID_TRANSITION`.
- **Lease expiry fails the attempt, then retries within budget.** The table has no `leased -> ready`, so an expired
  lease goes `failed`, and the reconciler issues the retry after backoff (T05).
- **Stale inputs during validation.** The contract is flagged and `accept` returns `409 STALE_INPUT`; the reconciler
  then moves it to `rejected` (T14).
- **`If-Match` is optional on lease-fenced worker commands** (`claim`, `heartbeat`, `start`, `artifacts`, `submit`,
  `fail`, effects). The lease token and fencing token are the concurrency guard there. It is required on everything
  a person does to an existing object.
- **Revising an accepted contract creates a successor** linked by `supersedes_contract_id`. Only a contract in that
  chain may publish a new version of the artifact. `revise` edits in place only while `proposed`, `blocked`,
  `rejected` or `failed`.
- **Goal revisions.** The human owner revises directly; anyone else creates a proposal. A scope change flags active
  and accepted contracts for reassessment and holds only the contracts named in `hold_contract_ids`.
- **Decisions hold dependents, not the evidence.** Contracts already producing keep their proposals but cannot be
  accepted while the decision is open.
- **Scope checks are a four-way intersection**: requester's grant, agent's grant, contract `permission_scope`, and
  the goal's resource policy. A goal with no resource policy allows no scopes.
- **Capability matching for automatic assignment** uses the deliverable type as the capability key. Capabilities
  never grant anything.

## Routes added to the spec's list

`/v1/principals`, `/v1/grants`, `/v1/goals/{id}/resource-policy`, `/v1/me`; goal `pause`, `complete`, `cancel`,
`revisions/{id}/approve|reject`, `artifacts` (human-published goal inputs); contract `revalidate`, `fail`,
`effects` and `effects/{id}/result`; `/v1/effects/{id}/recover`; `/v1/conflicts` with `rounds` and `escalate`;
decision `refresh` and `cancel`; `/v1/events/{id}`, `/v1/artifacts/...`, `/v1/blobs`, `/v1/notifications`,
`/v1/audit`, `/v1/admin/tick`. OpenAPI is at `/docs`.

## Not built

- A2A adapter and MCP gateway integration (spec M4, "if required").
- Fault-injection and load tests, and measurement of the §13 latency targets.
- SSE event streams (polling only), external identity providers (credentials are opaque bearer tokens), object
  storage other than the local filesystem, audit retention and PII rules.
- A security review. Do not put real customer data in this pilot build.
