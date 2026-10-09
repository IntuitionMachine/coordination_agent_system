"""Domain errors. Each maps to one HTTP status and a stable machine-readable code (spec §5)."""
from __future__ import annotations

from typing import Any


class CasError(Exception):
    status = 500
    code = "INTERNAL_ERROR"

    def __init__(self, message: str, *, code: str | None = None, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.message = message
        if code:
            self.code = code
        self.details = details or {}


class NotFound(CasError):
    status = 404
    code = "NOT_FOUND"


class Unauthenticated(CasError):
    status = 401
    code = "UNAUTHENTICATED"


class PolicyDenied(CasError):
    """403. Carries enough context for the API layer to audit and escalate the denial."""
    status = 403
    code = "POLICY_DENIED"

    def __init__(self, message: str, *, action: str = "", resource_type: str = "", resource_id: str | None = None,
                 goal_id: str | None = None, escalate: bool = False, details: dict[str, Any] | None = None):
        super().__init__(message, details=details)
        self.action = action
        self.resource_type = resource_type
        self.resource_id = resource_id
        self.goal_id = goal_id
        self.escalate = escalate


class Conflict(CasError):
    status = 409
    code = "CONFLICT"


class VersionConflict(Conflict):
    code = "VERSION_CONFLICT"


class InvalidTransition(Conflict):
    code = "INVALID_TRANSITION"


class Unprocessable(CasError):
    status = 422
    code = "UNPROCESSABLE"


class PreconditionRequired(CasError):
    status = 428
    code = "PRECONDITION_REQUIRED"


class QuotaExceeded(CasError):
    status = 429
    code = "QUOTA_EXCEEDED"


class Unavailable(CasError):
    status = 503
    code = "DEPENDENCY_UNAVAILABLE"
