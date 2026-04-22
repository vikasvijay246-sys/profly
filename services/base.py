"""
BaseService — all service classes inherit from this.

Provides:
  • safe_commit()  — commits or rolls back, never leaks a dirty session
  • transaction()  — context manager for atomic multi-step operations
  • Structured logging bound to the service name
"""
import contextlib
from typing import Generator

from models import db
from utils.errors import ServiceError
from utils.logger import get_logger


class BaseService:
    """
    All PropFlow service classes inherit from BaseService.

    Rule: every public method that writes to the DB must use
    either `self.safe_commit()` or `self.transaction()`.
    """

    def __init__(self):
        self.log = get_logger(f"services.{type(self).__name__}")

    # ── Commit helper ──────────────────────────────────────────────────────────
    def safe_commit(self, context: str = "") -> None:
        """
        Flush + commit the current session.
        On any DB error: rollback, log with context, re-raise as ServiceError.
        """
        try:
            db.session.commit()
        except Exception as exc:
            db.session.rollback()
            self.log.error(
                f"DB commit failed: {context}",
                extra={"exception": str(exc), "exc_type": type(exc).__name__},
            )
            raise ServiceError(
                f"Database error while {context or 'saving changes'}. Please retry.",
                original=str(exc),
            ) from exc

    # ── Transaction context manager ────────────────────────────────────────────
    @contextlib.contextmanager
    def transaction(self, context: str = "") -> Generator[None, None, None]:
        """
        Usage:
            with self.transaction("creating payment"):
                db.session.add(obj)
                db.session.add(notif)
            # automatic commit on exit; rollback + ServiceError on any exception

        Never call db.session.commit() inside a `transaction()` block —
        the context manager handles it.
        """
        try:
            yield
            db.session.commit()
            if context:
                self.log.debug(f"Transaction committed: {context}")
        except ServiceError:
            # Already rolled back by a nested safe_commit or inner transaction
            raise
        except Exception as exc:
            db.session.rollback()
            self.log.error(
                f"Transaction failed: {context}",
                extra={"exception": str(exc), "exc_type": type(exc).__name__},
            )
            raise ServiceError(
                f"Operation failed during {context or 'database transaction'}.",
                original=str(exc),
            ) from exc

    # ── Guard helpers ──────────────────────────────────────────────────────────
    def _require(self, value, name: str):
        """Assert value is not None/empty, raise ServiceError otherwise."""
        if value is None:
            raise ServiceError(f"{name} must not be None")
        return value
