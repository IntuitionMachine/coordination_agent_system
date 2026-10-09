-- CAS v0.1 schema. Every table is tenant-scoped; primary and foreign keys carry tenant_id
-- so a row can never reference another tenant's data (spec §2, §9, T17).

CREATE TABLE tenants (
  id text PRIMARY KEY,
  name text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE principals (
  tenant_id text NOT NULL REFERENCES tenants(id),
  id text NOT NULL,
  kind text NOT NULL CHECK (kind IN ('human','agent','service')),
  display_name text NOT NULL,
  issuer text NOT NULL,
  external_subject text NOT NULL,
  status text NOT NULL DEFAULT 'active' CHECK (status IN ('active','suspended')),
  attention_preference text NOT NULL DEFAULT 'decision'
    CHECK (attention_preference IN ('ambient','digest','decision','interrupt')),
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  version integer NOT NULL DEFAULT 1,
  PRIMARY KEY (tenant_id, id),
  UNIQUE (tenant_id, issuer, external_subject)
);

CREATE TABLE api_tokens (
  token_hash text PRIMARY KEY,
  tenant_id text NOT NULL,
  principal_id text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  revoked_at timestamptz,
  FOREIGN KEY (tenant_id, principal_id) REFERENCES principals(tenant_id, id)
);

CREATE TABLE agent_capabilities (
  tenant_id text NOT NULL,
  id text NOT NULL,
  agent_id text NOT NULL,
  capability_key text NOT NULL,
  description text NOT NULL DEFAULT '',
  input_schema_ref text,
  output_schema_ref text,
  adapter_type text NOT NULL DEFAULT 'pull',
  adapter_config_ref text,
  limits jsonb NOT NULL DEFAULT '{}',
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  version integer NOT NULL DEFAULT 1,
  PRIMARY KEY (tenant_id, id),
  UNIQUE (tenant_id, agent_id, capability_key),
  FOREIGN KEY (tenant_id, agent_id) REFERENCES principals(tenant_id, id)
);

CREATE TABLE policy_grants (
  tenant_id text NOT NULL,
  id text NOT NULL,
  principal_id text NOT NULL,
  role text NOT NULL CHECK (role IN
    ('goal_owner','task_requester','task_owner','validator','decision_owner','admin','auditor')),
  resource_type text NOT NULL CHECK (resource_type IN ('tenant','goal')),
  resource_id text,
  scopes jsonb NOT NULL DEFAULT '[]',
  granted_by text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  revoked_at timestamptz,
  version integer NOT NULL DEFAULT 1,
  PRIMARY KEY (tenant_id, id),
  FOREIGN KEY (tenant_id, principal_id) REFERENCES principals(tenant_id, id)
);
CREATE INDEX policy_grants_principal ON policy_grants (tenant_id, principal_id) WHERE revoked_at IS NULL;

-- Resource layer of the authority intersection (spec §8). No row for a goal = no scope allowed.
CREATE TABLE resource_policies (
  tenant_id text NOT NULL,
  goal_id text NOT NULL,
  allowed_scopes jsonb NOT NULL DEFAULT '[]',
  updated_by text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  version integer NOT NULL DEFAULT 1,
  PRIMARY KEY (tenant_id, goal_id)
);

CREATE TABLE goals (
  tenant_id text NOT NULL,
  id text NOT NULL,
  title text NOT NULL,
  objective text NOT NULL,
  success_criteria jsonb NOT NULL,
  constraints jsonb NOT NULL,
  preferences jsonb NOT NULL,
  owner_principal_id text NOT NULL,
  status text NOT NULL DEFAULT 'draft'
    CHECK (status IN ('draft','active','paused','completed','cancelled')),
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  version integer NOT NULL DEFAULT 1,
  PRIMARY KEY (tenant_id, id),
  FOREIGN KEY (tenant_id, owner_principal_id) REFERENCES principals(tenant_id, id)
);

CREATE TABLE goal_revisions (
  tenant_id text NOT NULL,
  id text NOT NULL,
  goal_id text NOT NULL,
  base_version integer NOT NULL,
  proposed_by text NOT NULL,
  status text NOT NULL CHECK (status IN ('proposed','applied','rejected')),
  changes jsonb NOT NULL,
  rationale text NOT NULL DEFAULT '',
  hold_contract_ids jsonb NOT NULL DEFAULT '[]',
  decided_by text,
  decided_at timestamptz,
  decision_note text,
  resulting_version integer,
  before jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  version integer NOT NULL DEFAULT 1,
  PRIMARY KEY (tenant_id, id),
  FOREIGN KEY (tenant_id, goal_id) REFERENCES goals(tenant_id, id)
);

CREATE TABLE contracts (
  tenant_id text NOT NULL,
  id text NOT NULL,
  goal_id text NOT NULL,
  parent_contract_id text,
  supersedes_contract_id text,
  title text NOT NULL,
  objective text NOT NULL,
  requester_id text NOT NULL,
  owner_id text,
  assigned_owner_id text,
  status text NOT NULL DEFAULT 'proposed' CHECK (status IN
    ('proposed','ready','leased','running','blocked','submitted','validating',
     'accepted','rejected','failed','cancelled')),
  input_refs jsonb NOT NULL,
  pinned_inputs jsonb NOT NULL DEFAULT '[]',
  deliverable_spec jsonb NOT NULL,
  acceptance_checks jsonb NOT NULL,
  permission_scope jsonb NOT NULL,
  policy_version integer NOT NULL DEFAULT 1,
  priority integer NOT NULL DEFAULT 50,
  deadline timestamptz,
  attempt integer NOT NULL DEFAULT 1,
  max_attempts integer NOT NULL,
  escalation_policy jsonb NOT NULL,
  fencing_token bigint NOT NULL DEFAULT 0,
  blocked_reason text,
  status_reason text,
  stale_inputs boolean NOT NULL DEFAULT false,
  requires_reassessment boolean NOT NULL DEFAULT false,
  not_before timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  version integer NOT NULL DEFAULT 1,
  PRIMARY KEY (tenant_id, id),
  FOREIGN KEY (tenant_id, goal_id) REFERENCES goals(tenant_id, id),
  FOREIGN KEY (tenant_id, requester_id) REFERENCES principals(tenant_id, id),
  FOREIGN KEY (tenant_id, owner_id) REFERENCES principals(tenant_id, id),
  FOREIGN KEY (tenant_id, parent_contract_id) REFERENCES contracts(tenant_id, id),
  FOREIGN KEY (tenant_id, supersedes_contract_id) REFERENCES contracts(tenant_id, id)
);
CREATE INDEX contracts_goal_status ON contracts (tenant_id, goal_id, status);
CREATE INDEX contracts_status ON contracts (status);

CREATE TABLE contract_revisions (
  tenant_id text NOT NULL,
  id text NOT NULL,
  contract_id text NOT NULL,
  contract_version integer NOT NULL,
  actor_id text NOT NULL,
  before jsonb NOT NULL,
  after jsonb NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, id),
  FOREIGN KEY (tenant_id, contract_id) REFERENCES contracts(tenant_id, id)
);

CREATE TABLE contract_dependencies (
  tenant_id text NOT NULL,
  upstream_id text NOT NULL,
  downstream_id text NOT NULL,
  requirement text NOT NULL DEFAULT 'accepted' CHECK (requirement = 'accepted'),
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, upstream_id, downstream_id),
  CHECK (upstream_id <> downstream_id),
  FOREIGN KEY (tenant_id, upstream_id) REFERENCES contracts(tenant_id, id),
  FOREIGN KEY (tenant_id, downstream_id) REFERENCES contracts(tenant_id, id)
);
CREATE INDEX contract_dependencies_down ON contract_dependencies (tenant_id, downstream_id);

-- One row per claimed run. attempt_no is the retry-budget counter; fencing_token identifies the run.
CREATE TABLE contract_attempts (
  tenant_id text NOT NULL,
  id text NOT NULL,
  contract_id text NOT NULL,
  attempt_no integer NOT NULL,
  fencing_token bigint NOT NULL,
  owner_id text NOT NULL,
  status text NOT NULL CHECK (status IN
    ('leased','running','submitted','validating','accepted','rejected','failed','fenced','cancelled')),
  pinned_inputs jsonb NOT NULL DEFAULT '[]',
  outcome_reason text,
  started_at timestamptz NOT NULL DEFAULT now(),
  ended_at timestamptz,
  PRIMARY KEY (tenant_id, id),
  UNIQUE (tenant_id, contract_id, fencing_token),
  FOREIGN KEY (tenant_id, contract_id) REFERENCES contracts(tenant_id, id)
);

CREATE TABLE leases (
  tenant_id text NOT NULL,
  id text NOT NULL,
  contract_id text NOT NULL,
  fencing_token bigint NOT NULL,
  holder_id text NOT NULL,
  worker_id text NOT NULL,
  token text NOT NULL,
  status text NOT NULL CHECK (status IN ('active','released','expired','fenced')),
  expires_at timestamptz NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, id),
  UNIQUE (tenant_id, contract_id, fencing_token),
  FOREIGN KEY (tenant_id, contract_id) REFERENCES contracts(tenant_id, id)
);
-- At most one active lease per contract, enforced by the database (T04).
CREATE UNIQUE INDEX leases_one_active ON leases (tenant_id, contract_id) WHERE status = 'active';

CREATE TABLE artifacts (
  tenant_id text NOT NULL,
  id text NOT NULL,
  goal_id text NOT NULL,
  artifact_type text NOT NULL,
  origin_contract_id text,
  current_version_id text,
  version_counter integer NOT NULL DEFAULT 0,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  version integer NOT NULL DEFAULT 1,
  PRIMARY KEY (tenant_id, id),
  FOREIGN KEY (tenant_id, goal_id) REFERENCES goals(tenant_id, id)
);

CREATE TABLE artifact_versions (
  tenant_id text NOT NULL,
  artifact_id text NOT NULL,
  version_id text NOT NULL,
  version_no integer NOT NULL,
  contract_id text,
  fencing_token bigint,
  attempt_no integer,
  producer_id text NOT NULL,
  uri text NOT NULL,
  sha256 text NOT NULL,
  media_type text NOT NULL,
  schema_ref text,
  size_bytes bigint NOT NULL DEFAULT 0,
  input_lineage jsonb NOT NULL DEFAULT '[]',
  status text NOT NULL CHECK (status IN ('proposed','accepted','rejected','superseded')),
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  version integer NOT NULL DEFAULT 1,
  PRIMARY KEY (tenant_id, artifact_id, version_id),
  FOREIGN KEY (tenant_id, artifact_id) REFERENCES artifacts(tenant_id, id),
  FOREIGN KEY (tenant_id, contract_id) REFERENCES contracts(tenant_id, id)
);
CREATE INDEX artifact_versions_contract ON artifact_versions (tenant_id, contract_id);

CREATE TABLE validation_results (
  tenant_id text NOT NULL,
  id text NOT NULL,
  contract_id text NOT NULL,
  fencing_token bigint NOT NULL,
  check_key text NOT NULL,
  status text NOT NULL CHECK (status IN ('pass','fail')),
  validator_id text NOT NULL,
  evidence jsonb NOT NULL DEFAULT '{}',
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, id),
  FOREIGN KEY (tenant_id, contract_id) REFERENCES contracts(tenant_id, id)
);

CREATE TABLE conflicts (
  tenant_id text NOT NULL,
  id text NOT NULL,
  goal_id text NOT NULL,
  kind text NOT NULL CHECK (kind IN ('version_collision','dependency_invalid',
    'artifact_incompatibility','evidence_disagreement','priority_tradeoff','permission_denied')),
  contract_ids jsonb NOT NULL DEFAULT '[]',
  proposals jsonb NOT NULL DEFAULT '[]',
  description text NOT NULL DEFAULT '',
  rounds integer NOT NULL DEFAULT 0,
  max_rounds integer NOT NULL DEFAULT 2,
  evidence_fingerprints jsonb NOT NULL DEFAULT '[]',
  status text NOT NULL CHECK (status IN ('open','auto_resolved','escalated','resolved')),
  resolution jsonb,
  decision_id text,
  created_by text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  version integer NOT NULL DEFAULT 1,
  PRIMARY KEY (tenant_id, id),
  FOREIGN KEY (tenant_id, goal_id) REFERENCES goals(tenant_id, id)
);

CREATE TABLE decisions (
  tenant_id text NOT NULL,
  id text NOT NULL,
  goal_id text NOT NULL,
  contract_ids jsonb NOT NULL DEFAULT '[]',
  held_contract_ids jsonb NOT NULL DEFAULT '[]',
  question text NOT NULL,
  kind text NOT NULL CHECK (kind IN ('factual','preference','context','authority','other')),
  alternatives jsonb NOT NULL,
  evidence_refs jsonb NOT NULL DEFAULT '[]',
  baseline jsonb NOT NULL DEFAULT '[]',
  consequences text NOT NULL DEFAULT '',
  decision_owner_id text NOT NULL,
  state text NOT NULL CHECK (state IN ('open','awaiting_authority','resolved','superseded','cancelled')),
  resolution jsonb,
  conflict_id text,
  created_by text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  version integer NOT NULL DEFAULT 1,
  PRIMARY KEY (tenant_id, id),
  FOREIGN KEY (tenant_id, goal_id) REFERENCES goals(tenant_id, id),
  FOREIGN KEY (tenant_id, decision_owner_id) REFERENCES principals(tenant_id, id)
);

CREATE TABLE decision_resolutions (
  tenant_id text NOT NULL,
  id text NOT NULL,
  decision_id text NOT NULL,
  actor_id text NOT NULL,
  chosen_alternative text NOT NULL,
  rationale text NOT NULL,
  input_versions jsonb NOT NULL,
  impacted_contract_ids jsonb NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, id),
  FOREIGN KEY (tenant_id, decision_id) REFERENCES decisions(tenant_id, id)
);

-- External side effects requested through the action gateway (spec §8, T09, T16).
CREATE TABLE external_effects (
  tenant_id text NOT NULL,
  id text NOT NULL,
  contract_id text NOT NULL,
  fencing_token bigint NOT NULL,
  attempt_no integer NOT NULL,
  step_key text NOT NULL,
  scope text NOT NULL,
  idempotent boolean NOT NULL,
  status text NOT NULL CHECK (status IN
    ('authorized','succeeded','failed','manual_recovery_required','recovered')),
  detail jsonb NOT NULL DEFAULT '{}',
  actor_id text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  version integer NOT NULL DEFAULT 1,
  PRIMARY KEY (tenant_id, id),
  FOREIGN KEY (tenant_id, contract_id) REFERENCES contracts(tenant_id, id)
);
CREATE INDEX external_effects_contract ON external_effects (tenant_id, contract_id, step_key);

CREATE TABLE events (
  seq bigserial PRIMARY KEY,
  tenant_id text NOT NULL REFERENCES tenants(id),
  id text NOT NULL,
  source text NOT NULL,
  type text NOT NULL,
  subject text NOT NULL,
  time timestamptz NOT NULL DEFAULT now(),
  goal_id text,
  aggregate_type text NOT NULL,
  aggregate_id text NOT NULL,
  aggregate_version integer NOT NULL,
  actor_id text NOT NULL,
  correlation_id text NOT NULL,
  causation_id text,
  schema_version integer NOT NULL DEFAULT 1,
  payload jsonb NOT NULL DEFAULT '{}',
  UNIQUE (source, id)
);
CREATE INDEX events_goal ON events (tenant_id, goal_id, seq);
CREATE INDEX events_subject ON events (tenant_id, subject, seq);

CREATE TABLE outbox (
  seq bigserial PRIMARY KEY,
  tenant_id text NOT NULL,
  event_seq bigint NOT NULL REFERENCES events(seq),
  status text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','delivered','dead')),
  attempts integer NOT NULL DEFAULT 0,
  next_attempt_at timestamptz NOT NULL DEFAULT now(),
  last_error text,
  created_at timestamptz NOT NULL DEFAULT now(),
  delivered_at timestamptz
);
CREATE INDEX outbox_pending ON outbox (next_attempt_at) WHERE status = 'pending';

CREATE TABLE inbox_dedup (
  consumer text NOT NULL,
  source text NOT NULL,
  event_id text NOT NULL,
  tenant_id text NOT NULL,
  processed_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (consumer, source, event_id)
);

CREATE TABLE consumer_aggregate_versions (
  consumer text NOT NULL,
  tenant_id text NOT NULL,
  aggregate_type text NOT NULL,
  aggregate_id text NOT NULL,
  last_version integer NOT NULL,
  PRIMARY KEY (consumer, tenant_id, aggregate_type, aggregate_id)
);

CREATE TABLE idempotency_records (
  tenant_id text NOT NULL,
  actor_id text NOT NULL,
  key text NOT NULL,
  fingerprint text NOT NULL,
  status_code integer,
  response jsonb,
  headers jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, actor_id, key)
);

CREATE TABLE audit_actions (
  tenant_id text NOT NULL,
  id text NOT NULL,
  actor_id text NOT NULL,
  action text NOT NULL,
  resource_type text NOT NULL,
  resource_id text,
  goal_id text,
  decision text NOT NULL CHECK (decision IN ('allow','deny','error')),
  reason text,
  details jsonb NOT NULL DEFAULT '{}',
  request_id text,
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, id)
);
CREATE INDEX audit_actions_goal ON audit_actions (tenant_id, goal_id, created_at);

CREATE TABLE notifications (
  tenant_id text NOT NULL,
  id text NOT NULL,
  recipient_id text NOT NULL,
  goal_id text,
  event_id text NOT NULL,
  event_type text NOT NULL,
  subject text NOT NULL,
  level text NOT NULL CHECK (level IN ('ambient','digest','decision','interrupt')),
  mandated boolean NOT NULL DEFAULT false,
  reason text NOT NULL,
  summary text NOT NULL,
  read_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, id),
  UNIQUE (tenant_id, recipient_id, event_id)
);

-- Runs started by the in-process scheduler through an adapter (spec §6).
CREATE TABLE adapter_runs (
  tenant_id text NOT NULL,
  id text NOT NULL,
  contract_id text NOT NULL,
  fencing_token bigint NOT NULL,
  agent_id text NOT NULL,
  adapter_type text NOT NULL,
  external_run_id text,
  lease_token text NOT NULL,
  status text NOT NULL CHECK (status IN ('starting','running','finished','failed','cancelled')),
  detail jsonb NOT NULL DEFAULT '{}',
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, id),
  FOREIGN KEY (tenant_id, contract_id) REFERENCES contracts(tenant_id, id)
);

-- Counters maintained by an outbox consumer (spec §13 metrics). Deduplicated, so redelivery
-- never double-counts (T15).
CREATE TABLE metrics_counters (
  tenant_id text NOT NULL,
  goal_id text NOT NULL,
  metric text NOT NULL,
  value bigint NOT NULL DEFAULT 0,
  updated_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, goal_id, metric)
);
