"""Runtime configuration, read from environment variables."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _int(name: str, default: int) -> int:
    return int(os.environ.get(name, default))


def _float(name: str, default: float) -> float:
    return float(os.environ.get(name, default))


@dataclass
class Settings:
    # Empty database_url means "start the embedded development PostgreSQL in data_dir".
    database_url: str = field(default_factory=lambda: os.environ.get("CAS_DATABASE_URL", ""))
    data_dir: Path = field(default_factory=lambda: Path(os.environ.get("CAS_DATA_DIR", ".cas-data")).resolve())
    blob_dir: str = field(default_factory=lambda: os.environ.get("CAS_BLOB_DIR", ""))
    adapter_config: str = field(default_factory=lambda: os.environ.get("CAS_ADAPTER_CONFIG", ""))

    lease_default_seconds: int = field(default_factory=lambda: _int("CAS_LEASE_DEFAULT_SECONDS", 60))
    lease_max_seconds: int = field(default_factory=lambda: _int("CAS_LEASE_MAX_SECONDS", 3600))
    retry_backoff_base_seconds: float = field(default_factory=lambda: _float("CAS_RETRY_BACKOFF_BASE_SECONDS", 2.0))
    retry_backoff_max_seconds: float = field(default_factory=lambda: _float("CAS_RETRY_BACKOFF_MAX_SECONDS", 300.0))
    conflict_max_rounds: int = field(default_factory=lambda: _int("CAS_CONFLICT_MAX_ROUNDS", 2))
    idempotency_retention_hours: int = field(default_factory=lambda: _int("CAS_IDEMPOTENCY_RETENTION_HOURS", 24))
    outbox_max_attempts: int = field(default_factory=lambda: _int("CAS_OUTBOX_MAX_ATTEMPTS", 8))
    worker_tick_seconds: float = field(default_factory=lambda: _float("CAS_WORKER_TICK_SECONDS", 1.0))
    max_blob_bytes: int = field(default_factory=lambda: _int("CAS_MAX_BLOB_BYTES", 10 * 1024 * 1024))

    @property
    def blob_path(self) -> Path:
        return Path(self.blob_dir).resolve() if self.blob_dir else self.data_dir / "blobs"


settings = Settings()
