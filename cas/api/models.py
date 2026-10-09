"""Request bodies for commands that are not covered by the spec's JSON Schemas."""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Capability(Body):
    capability_key: str = Field(min_length=1)
    description: str = ""
    input_schema_ref: str | None = None
    output_schema_ref: str | None = None
    adapter_type: str = "pull"
    adapter_config_ref: str | None = None
    limits: dict[str, Any] = {}


class AgentCreate(Body):
    id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_.-]{1,64}$")
    display_name: str = Field(min_length=1)
    external_subject: str = Field(min_length=1)
    issuer: str | None = None
    capabilities: list[Capability] = []
    issue_token: bool = True


class PrincipalCreate(Body):
    id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_.-]{1,64}$")
    kind: Literal["human", "service"]
    display_name: str = Field(min_length=1)
    external_subject: str = Field(min_length=1)
    issuer: str | None = None
    attention_preference: Literal["ambient", "digest", "decision", "interrupt"] | None = None
    issue_token: bool = True


class GrantCreate(Body):
    principal_id: str
    role: str
    goal_id: str | None = None
    scopes: list[str] = []


class ResourcePolicy(Body):
    allowed_scopes: list[str]


class Attention(Body):
    level: Literal["ambient", "digest", "decision", "interrupt"]


class Note(Body):
    reason: str = ""


class GoalRevision(Body):
    changes: dict[str, Any]
    rationale: str = ""
    hold_contract_ids: list[str] = []


class RevisionDecision(Body):
    note: str = ""


class ContractRevision(Body):
    changes: dict[str, Any] = {}
    rationale: str = ""


class Dependency(Body):
    upstream_id: str = Field(min_length=1)


class Claim(Body):
    worker_id: str = Field(min_length=1, max_length=200)
    lease_seconds: int | None = Field(default=None, ge=1)


class Lease(Body):
    lease_token: str = Field(min_length=1)


class Heartbeat(Lease):
    lease_seconds: int | None = Field(default=None, ge=1)


class Block(Body):
    reason: str = ""
    lease_token: str | None = None


class Fail(Lease):
    reason: str = ""
    retryable: bool = False


class ArtifactContent(Body):
    artifact_id: str | None = None
    type: str = Field(min_length=1)
    media_type: str | None = None
    schema_ref: str | None = None
    content_text: str | None = None
    content_json: Any | None = None
    content_base64: str | None = None
    uri: str | None = None
    sha256: str | None = None


class ArtifactRegister(ArtifactContent):
    lease_token: str = Field(min_length=1)


class Blob(Body):
    content_base64: str


class CheckResult(Body):
    check: str = Field(min_length=1)
    status: Literal["pass", "fail"]
    evidence: dict[str, Any] = {}


class Accept(Body):
    check_results: list[CheckResult] = []


class Reject(Body):
    reason: str = ""
    check_results: list[CheckResult] = []


class EffectRequest(Lease):
    step_key: str = Field(min_length=1, max_length=200)
    scope: str = Field(min_length=1)
    idempotent: bool = False
    detail: dict[str, Any] = {}


class EffectResult(Lease):
    status: Literal["succeeded", "failed"]
    detail: dict[str, Any] = {}


class EffectRecovery(Body):
    outcome: Literal["applied", "not_applied"]
    note: str = Field(min_length=1)


class Alternative(Body):
    id: str = Field(min_length=1)
    label: str = Field(min_length=1)
    consequences: str = ""


class EvidenceRef(Body):
    artifact_id: str = Field(min_length=1)
    version_id: str = Field(min_length=1)


class DecisionCreate(Body):
    goal_id: str
    contract_ids: list[str] = []
    question: str = Field(min_length=1)
    kind: Literal["factual", "preference", "context", "authority", "other"] = "other"
    alternatives: list[Alternative] = Field(min_length=1)
    evidence_refs: list[EvidenceRef] = []
    consequences: str = ""
    decision_owner_id: str
    hold_dependents: bool = True


class DecisionResolve(Body):
    chosen_alternative: str = Field(min_length=1)
    rationale: str = Field(min_length=1)


class DecisionRefresh(Body):
    evidence_refs: list[EvidenceRef] = []


class ConflictCreate(Body):
    goal_id: str
    kind: Literal["version_collision", "dependency_invalid", "artifact_incompatibility",
                  "evidence_disagreement", "priority_tradeoff", "permission_denied"]
    contract_ids: list[str] = []
    proposals: list[EvidenceRef] = []
    evidence_refs: list[EvidenceRef] = []
    description: str = ""
    decision_owner_id: str | None = None


class ConflictRound(Body):
    proposals: list[EvidenceRef] = []
    evidence_refs: list[EvidenceRef] = []
    note: str = ""


class ConflictEscalate(Body):
    question: str | None = None
    decision_owner_id: str | None = None
    consequences: str = ""
    alternatives: list[Alternative] = []


class MarkRead(Body):
    ids: list[str]
