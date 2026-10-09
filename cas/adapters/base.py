"""The adapter contract between CAS and an agent runtime (spec §6).

CAS decides what work exists and who holds it; an adapter only starts, watches, cancels and
collects one run. Adapters never get to accept their own output."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from cas.config import settings


@dataclass
class CapabilityDescriptor:
    capability_key: str
    description: str = ""
    input_schema_ref: str | None = None
    output_schema_ref: str | None = None


@dataclass
class PollResult:
    state: str  # 'progress' | 'submitted' | 'error'
    detail: str = ""
    retryable: bool = True


@dataclass
class RunRequest:
    contract: dict[str, Any]
    pinned_inputs: list[dict[str, Any]]   # each pin plus its media_type and content
    lease_token: str
    policy_context: dict[str, Any]        # effective scopes for this agent on this contract
    config: dict[str, Any] = field(default_factory=dict)


class Adapter(Protocol):
    adapter_type: str

    def capabilities(self) -> list[CapabilityDescriptor]: ...
    def start(self, request: RunRequest) -> str: ...
    def poll(self, external_run_id: str) -> PollResult: ...
    def cancel(self, external_run_id: str) -> bool: ...
    def collect(self, external_run_id: str) -> list[dict[str, Any]]: ...


def load_adapter_configs() -> dict[str, dict]:
    """Adapter configuration lives in a local file, keyed by the adapter_config_ref stored on a
    capability. The database only ever holds the reference, so a row cannot inject a command."""
    path = settings.adapter_config
    if not path or not Path(path).exists():
        return {}
    return json.loads(Path(path).read_text())
