"""Immutable, content-addressed artifact storage on the local filesystem (spec §9).

Bytes are stored under their SHA-256, so a published blob can never change. The store is
partitioned by tenant; a tenant can only read blobs it wrote.
"""
from __future__ import annotations

import hashlib
import os
import re
import tempfile
from pathlib import Path

from cas.config import settings

URI_PREFIX = "cas://sha256/"
_HEX = re.compile(r"^[0-9a-f]{64}$")
_SAFE = re.compile(r"^[A-Za-z0-9_.-]+$")


def _path(tenant_id: str, sha256: str) -> Path:
    if not _HEX.match(sha256) or not _SAFE.match(tenant_id):
        raise ValueError("invalid blob address")
    return settings.blob_path / tenant_id / sha256[:2] / sha256


def uri_for(sha256: str) -> str:
    return URI_PREFIX + sha256


def sha_from_uri(uri: str) -> str | None:
    if uri.startswith(URI_PREFIX) and _HEX.match(uri[len(URI_PREFIX):]):
        return uri[len(URI_PREFIX):]
    return None


def put(tenant_id: str, data: bytes) -> tuple[str, str]:
    """Store bytes; returns (uri, sha256). Writing the same bytes twice is a no-op."""
    sha = hashlib.sha256(data).hexdigest()
    path = _path(tenant_id, sha)
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent)
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
            os.replace(tmp, path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
        os.chmod(path, 0o444)
    return uri_for(sha), sha


def get(tenant_id: str, sha256: str) -> bytes | None:
    try:
        path = _path(tenant_id, sha256)
    except ValueError:
        return None
    if not path.exists():
        return None
    data = path.read_bytes()
    if hashlib.sha256(data).hexdigest() != sha256:
        raise IOError(f"blob {sha256} failed its integrity check")
    return data


def exists(tenant_id: str, sha256: str) -> bool:
    try:
        return _path(tenant_id, sha256).exists()
    except ValueError:
        return False
