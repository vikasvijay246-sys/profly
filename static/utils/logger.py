"""
Structured JSON logger for PropFlow.

Usage:
    from utils.logger import get_logger
    log = get_logger(__name__)
    log.info("Payment created", extra={"payment_id": 5, "tenant_id": 2})
    log.error("Delete failed", extra={"user_id": 3, "reason": str(e)})
"""
import logging
import sys
import json
from datetime import datetime, timezone

# All standard LogRecord attribute names — never use these as extra= keys.
# If a caller passes one of these, we prefix it with "ctx_" to avoid the crash.
_RESERVED = frozenset({
    "args", "created", "exc_info", "exc_text", "filename", "funcName",
    "levelname", "levelno", "lineno", "message", "module", "msecs",
    "msg", "name", "pathname", "process", "processName", "relativeCreated",
    "stack_info", "taskName", "thread", "threadName",
})


class JSONFormatter(logging.Formatter):
    """Emit one JSON line per log record."""

    def format(self, record: logging.LogRecord) -> str:
        log_obj: dict = {
            "ts":     datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "level":  record.levelname,
            "logger": record.name,
            "msg":    record.getMessage(),
        }

        # Merge extra= fields, renaming any that clash with _RESERVED
        for key, value in record.__dict__.items():
            if key in _RESERVED:
                continue
            # Skip internal Python logging attributes
            if key.startswith("_") or key in {"args", "msg", "message"}:
                continue
            log_obj[key] = value

        if record.exc_info:
            log_obj["exception"] = self.formatException(record.exc_info)

        return json.dumps(log_obj, default=str, ensure_ascii=False)


def configure_logging(level: str = "INFO") -> None:
    """Call once at application startup."""
    root = logging.getLogger()
    root.handlers.clear()
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JSONFormatter())
    root.addHandler(handler)
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    for lib in ("werkzeug", "engineio", "socketio", "urllib3"):
        logging.getLogger(lib).setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
