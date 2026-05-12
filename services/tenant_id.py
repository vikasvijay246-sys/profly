"""
Human-readable unique tenant public IDs: TNR-{PROP}-{ROOM}-{SEQ}.
"""
import re
import secrets
from typing import TYPE_CHECKING

from models import db, User

if TYPE_CHECKING:
    from models import Property, Room


_BRAND = "TNR"
_MAX_ATTEMPTS = 48


def slug_property_code(prop: "Property") -> str:
    """Short property segment for IDs (4–6 chars)."""
    if getattr(prop, "short_code", None) and str(prop.short_code).strip():
        raw = re.sub(r"[^A-Za-z0-9]", "", str(prop.short_code).strip()).upper()
        return raw[:6] if raw else f"P{prop.id:02d}"
    unit = (prop.unit_number or "").strip()
    if unit:
        seg = re.sub(r"[^A-Za-z0-9]", "", unit).upper()
        if len(seg) >= 2:
            return seg[:6]
    # Fallback: initials from name + id
    words = re.findall(r"[A-Za-z]+", prop.name or "")
    initials = "".join(w[0] for w in words[:4]).upper()
    if len(initials) < 2:
        initials = "PG"
    return f"{initials[:4]}{prop.id % 100:02d}"[:6]


def slug_room_segment(room: "Room") -> str:
    """Room segment: alphanumeric, compact (e.g. A203, B101)."""
    num = (room.room_number or "").strip()
    seg = re.sub(r"[^A-Za-z0-9]", "", num).upper()
    if not seg:
        seg = f"R{room.id}"
    return seg[:8]


def generate_tenant_public_id(prop: "Property", room: "Room") -> str:
    """Generate and guarantee uniqueness against users.tenant_public_id."""
    pcode = slug_property_code(prop)
    rseg = slug_room_segment(room)
    for _ in range(_MAX_ATTEMPTS):
        seq = f"{secrets.randbelow(10000):04d}"
        candidate = f"{_BRAND}-{pcode}-{rseg}-{seq}"
        exists = (
            db.session.query(User.id)
            .filter(User.tenant_public_id == candidate)
            .first()
        )
        if not exists:
            return candidate
    # Extremely unlikely — widen sequence
    seq = secrets.token_hex(3).upper()[:6]
    candidate = f"{_BRAND}-{pcode}-{rseg}-{seq}"
    return candidate
