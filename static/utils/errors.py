"""
Centralised error types for PropFlow.

All service methods raise these; routes catch them and convert to
HTTP responses via `api_error()` or flash messages.

Usage:
    raise ValidationError("Amount must be positive")
    raise NotFoundError("Tenant", tenant_id)
    raise CapacityError("Room 101 is full (4/4)")
    raise ConflictError("Payment for 2025-06 already exists")
    raise PermissionError_("Only the owner can modify this tenant")
"""
from flask import jsonify
from utils.logger import get_logger

log = get_logger(__name__)


# ── Base ──────────────────────────────────────────────────────────────────────
class AppError(Exception):
    """Base class — all PropFlow errors inherit from this."""
    http_status: int = 400
    error_code:  str = "APP_ERROR"

    def __init__(self, message: str, **context):
        super().__init__(message)
        self.message = message
        self.context = context   # extra metadata logged but not exposed to user

    def to_dict(self) -> dict:
        return {
            "ok":    False,
            "code":  self.error_code,
            "error": self.message,
        }

    def log(self, logger=None):
        (logger or log).warning(
            self.message,
            extra={"error_code": self.error_code, **self.context},
        )


# ── Concrete error types ──────────────────────────────────────────────────────
class ValidationError(AppError):
    """Bad input: missing field, wrong type, out-of-range value."""
    http_status = 422
    error_code  = "VALIDATION_ERROR"


class NotFoundError(AppError):
    """Requested record does not exist (or was soft-deleted)."""
    http_status = 404
    error_code  = "NOT_FOUND"

    def __init__(self, entity: str, entity_id=None):
        msg = f"{entity} not found"
        if entity_id is not None:
            msg += f" (id={entity_id})"
        super().__init__(msg, entity=entity, entity_id=entity_id)


class ConflictError(AppError):
    """Operation would create a duplicate or violate a unique constraint."""
    http_status = 409
    error_code  = "CONFLICT"


class CapacityError(AppError):
    """Room is full; cannot add more tenants."""
    http_status = 409
    error_code  = "CAPACITY_EXCEEDED"


class PermissionError_(AppError):
    """Caller does not own / have rights to the target resource."""
    http_status = 403
    error_code  = "PERMISSION_DENIED"


class IntegrityError(AppError):
    """DB constraint would be violated; operation aborted."""
    http_status = 409
    error_code  = "INTEGRITY_ERROR"


class ServiceError(AppError):
    """Unexpected internal failure in a service method."""
    http_status = 500
    error_code  = "SERVICE_ERROR"


# ── HTTP response helpers ─────────────────────────────────────────────────────
def api_error(err: AppError):
    """Convert AppError → Flask JSON response."""
    err.log()
    return jsonify(err.to_dict()), err.http_status


def api_ok(data: dict = None, message: str = "OK", status: int = 200):
    """Standard success response."""
    payload = {"ok": True, "message": message}
    if data:
        payload["data"] = data
    return jsonify(payload), status


def handle_unexpected(exc: Exception, context: str = ""):
    """
    Log an unexpected exception and return a safe 500 response.
    Never exposes internal details to the client.
    """
    log.exception(
        f"Unexpected error: {context}",
        extra={"exception_type": type(exc).__name__},
    )
    return jsonify({
        "ok":    False,
        "code":  "INTERNAL_ERROR",
        "error": "An internal error occurred. Please try again.",
    }), 500
