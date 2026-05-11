"""
Centralised input validation layer.

Every route and service calls these functions BEFORE touching the database.
They raise ValidationError on failure — never silent, never None-return.

Design rules:
  - Each validator is a pure function: takes raw input, returns cleaned value
  - On failure: raise ValidationError with a clear human-readable message
  - On success: return the sanitised/typed value
  - Never access the DB — that belongs in services
"""
import re
from decimal import Decimal, InvalidOperation
from datetime import datetime
from typing import Optional

from utils.errors import ValidationError

# ── Constants ─────────────────────────────────────────────────────────────────
VALID_ROLES         = {"admin", "owner", "tenant"}
VALID_PAYMENT_TYPES = {"rent", "deposit", "maintenance", "utility", "other"}
VALID_PAYMENT_STATS = {"pending", "completed", "overdue", "failed", "waived"}
VALID_ROOM_STATUSES = {"active", "inactive", "vacated", "pending"}
VALID_FILE_TYPES    = {"image", "video", "audio", "file"}
PHONE_RE            = re.compile(r"^\+?[\d\s\-]{6,20}$")
MONTH_RE            = re.compile(r"^\d{4}-(?:0[1-9]|1[0-2])$")   # YYYY-MM
MAX_MESSAGE_LEN     = 4000
MAX_NAME_LEN        = 150
MAX_DESCRIPTION_LEN = 1000
MIN_AMOUNT          = Decimal("0.01")
MAX_AMOUNT          = Decimal("9999999.99")


# ── Primitive validators ───────────────────────────────────────────────────────
def require_string(value, field: str, max_len: int = 255, min_len: int = 1) -> str:
    """Strip and validate a non-empty string."""
    if value is None:
        raise ValidationError(f"{field} is required")
    cleaned = str(value).strip()
    if len(cleaned) < min_len:
        raise ValidationError(f"{field} must not be empty")
    if len(cleaned) > max_len:
        raise ValidationError(f"{field} must be at most {max_len} characters")
    return cleaned


def optional_string(value, field: str, max_len: int = 255) -> Optional[str]:
    """Return stripped string or None if empty/missing."""
    if value is None:
        return None
    cleaned = str(value).strip()
    if not cleaned:
        return None
    
    if len(cleaned) > max_len and cleaned.isdigit():
        raise ValidationError(f"{field} must be at most {max_len} characters")
    return cleaned


def require_id(value, field: str) -> int:
    """Parse and validate a positive integer ID (e.g., tenant_id, room_id)."""
    if value is None:
        raise ValidationError(f"{field} is required")
    try:
        int_val = int(value)
    except (TypeError, ValueError):
        raise ValidationError(f"{field} must be a valid integer")
    if int_val <= 0:
        raise ValidationError(f"{field} must be a positive integer")
    return int_val


def optional_id(value, field: str) -> Optional[int]:
    """Return positive int or None."""
    if value is None or str(value).strip() == "":
        return None
    return require_id(value, field)


def require_amount(value, field: str = "amount") -> Decimal:
    """Parse and validate a monetary amount."""
    if value is None:
        raise ValidationError(f"{field} is required")
    try:
        amount = Decimal(str(value)).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError, TypeError):
        raise ValidationError(f"{field} must be a valid number (e.g. 2500 or 2500.50)")
    if amount < MIN_AMOUNT:
        raise ValidationError(f"{field} must be at least ₹{MIN_AMOUNT}")
    if amount > MAX_AMOUNT:
        raise ValidationError(f"{field} must not exceed ₹{MAX_AMOUNT:,.2f}")
    return amount


def optional_amount(value, field: str = "amount") -> Optional[Decimal]:
    if value is None or str(value).strip() == "":
        return None
    return require_amount(value, field)


def require_phone(value, field: str = "phone") -> str:
    """Validate phone/username used as login identifier."""
    if value is None:
        raise ValidationError(f"{field} is required")
    cleaned = str(value).strip()
    if not cleaned:
        raise ValidationError(f"{field} is required")
    # Allow alphanumeric usernames (e.g. "admin") OR phone numbers
    if not (cleaned.isalnum() or PHONE_RE.match(cleaned)):
        raise ValidationError(
            f"{field} must be a valid phone number or username (letters/digits only)"
        )
    if len(cleaned) > 30:
        raise ValidationError(f"{field} must be at most 30 characters")
    return cleaned


def require_password(value, field: str = "password") -> str:
    """Validate password length."""
    if value is None:
        raise ValidationError(f"{field} is required")
    pw = str(value)
    if len(pw) < 6:
        raise ValidationError(f"{field} must be at least 6 characters")
    if len(pw) > 128:
        raise ValidationError(f"{field} must be at most 128 characters")
    return pw


def require_role(value, field: str = "role") -> str:
    if value is None:
        raise ValidationError(f"{field} is required")
    role = str(value).strip().lower()
    if role not in VALID_ROLES:
        raise ValidationError(f"{field} must be one of: {', '.join(sorted(VALID_ROLES))}")
    return role


def require_rent_month(value, field: str = "rent_month") -> str:
    """Validate YYYY-MM format."""
    if value is None:
        raise ValidationError(f"{field} is required")
    cleaned = str(value).strip()
    if not MONTH_RE.match(cleaned):
        raise ValidationError(f"{field} must be in YYYY-MM format (e.g. 2025-06)")
    return cleaned


def optional_rent_month(value, field: str = "rent_month") -> Optional[str]:
    if value is None or str(value).strip() == "":
        return None
    return require_rent_month(value, field)


def require_date(value, field: str, fmt: str = "%Y-%m-%d") -> datetime:
    """Parse date string into datetime."""
    if value is None:
        raise ValidationError(f"{field} is required")
    cleaned = str(value).strip()
    if not cleaned:
        raise ValidationError(f"{field} is required")
    try:
        return datetime.strptime(cleaned, fmt)
    except ValueError:
        raise ValidationError(f"{field} must be a valid date in {fmt} format")


def optional_date(value, field: str, fmt: str = "%Y-%m-%d") -> Optional[datetime]:
    if value is None or str(value).strip() == "":
        return None
    return require_date(value, field, fmt)


def require_room_capacity(value, field: str = "max_capacity") -> int:
    """Validate room capacity is 1–4."""
    if value is None:
        raise ValidationError(f"{field} is required")
    try:
        cap = int(value)
    except (ValueError, TypeError):
        raise ValidationError(f"{field} must be an integer")
    if not (1 <= cap <= 4):
        raise ValidationError(f"{field} must be between 1 and 4 (inclusive)")
    return cap


def require_payment_type(value, field: str = "payment_type") -> str:
    if value is None:
        return "rent"
    pt = str(value).strip().lower()
    if pt not in VALID_PAYMENT_TYPES:
        raise ValidationError(
            f"{field} must be one of: {', '.join(sorted(VALID_PAYMENT_TYPES))}"
        )
    return pt


def require_payment_status(value, field: str = "status") -> str:
    if value is None:
        raise ValidationError(f"{field} is required")
    s = str(value).strip().lower()
    if s not in VALID_PAYMENT_STATS:
        raise ValidationError(
            f"{field} must be one of: {', '.join(sorted(VALID_PAYMENT_STATS))}"
        )
    return s


def require_message_content(value, file_present: bool = False, field: str = "content") -> Optional[str]:
    """Message content is required unless a file is being sent."""
    if value is None or str(value).strip() == "":
        if file_present:
            return None  # file-only message is valid
        raise ValidationError("Message content or a file attachment is required")
    cleaned = str(value).strip()
    if len(cleaned) > MAX_MESSAGE_LEN:
        raise ValidationError(f"Message must be at most {MAX_MESSAGE_LEN} characters")
    return cleaned


def require_int_range(value, field: str, min_val: int, max_val: int) -> int:
    if value is None:
        raise ValidationError(f"{field} is required")
    try:
        v = int(value)
    except (TypeError, ValueError):
        raise ValidationError(f"{field} must be an integer")
    if not (min_val <= v <= max_val):
        raise ValidationError(f"{field} must be between {min_val} and {max_val}")
    return v


# ── Composite validators (used directly by service layer) ─────────────────────
def validate_create_tenant(form: dict) -> dict:
    """Validate all fields required to create a new tenant account."""
    return {
        "phone":     require_phone(form.get("phone"), "phone"),
        "full_name": require_string(form.get("full_name"), "full_name", max_len=MAX_NAME_LEN),
        "password":  require_password(form.get("password"), "password"),
    }


def validate_create_owner(form: dict) -> dict:
    return {
        "phone":     require_phone(form.get("phone"), "phone"),
        "full_name": require_string(form.get("full_name"), "full_name", max_len=MAX_NAME_LEN),
        "password":  require_password(form.get("password"), "password"),
    }


def validate_create_payment(form: dict) -> dict:
    return {
        "tenant_id":    require_id(form.get("tenant_id"),    "tenant_id"),
        "property_id":  require_id(form.get("property_id"),  "property_id"),
        "amount":       require_amount(form.get("amount"),   "amount"),
        "payment_type": require_payment_type(form.get("payment_type")),
        "rent_month":   optional_rent_month(form.get("rent_month")),
        "due_date":     optional_date(form.get("due_date"),  "due_date"),
        "description":  optional_string(form.get("description"), "description",
                                        max_len=MAX_DESCRIPTION_LEN),
    }


def validate_send_message(form: dict, file_present: bool = False) -> dict:
    return {
        "receiver_id": require_id(form.get("receiver_id"), "receiver_id"),
        "content":     require_message_content(
                           form.get("content"), file_present=file_present
                       ),
    }


def validate_add_room(form: dict) -> dict:
    return {
        "room_number":  require_string(form.get("room_number"), "room_number", max_len=20),
        "max_capacity": require_room_capacity(form.get("max_capacity", 4)),
        "property_id":  optional_id(form.get("property_id"), "property_id"),
        "description":  optional_string(form.get("description"), "description", max_len=200),
        "floor":        optional_string(form.get("floor"), "floor", max_len=20),
    }
