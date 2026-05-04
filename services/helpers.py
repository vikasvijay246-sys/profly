"""
Pure utility functions used across service classes.
No DB access, no Flask context required.
"""
import os
import re
from datetime import datetime, timezone
from werkzeug.utils import secure_filename


def fmt_month(dt=None) -> str:
    """Return 'YYYY-MM' for given datetime (or UTC now)."""
    d = dt or datetime.now(timezone.utc)
    return d.strftime("%Y-%m")


def current_rent_month() -> str:
    return fmt_month()


def parse_month(s: str):
    """'2025-06' → (2025, 6). Raises ValueError on bad input."""
    parts = s.split("-")
    if len(parts) != 2:
        raise ValueError(f"Invalid month format: {s!r}")
    return int(parts[0]), int(parts[1])


def month_due_date(year: int, month: int) -> datetime:
    """Return the 1st of the month as a naive UTC datetime."""
    return datetime(year, month, 1, 0, 0, 0)


def next_month(year: int, month: int):
    """Return (year, month) for the month after the given one."""
    if month == 12:
        return year + 1, 1
    return year, month + 1


# ── File Upload Helpers ──────────────────────────────────────────────────────
ALLOWED_PHOTO_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'webp'}
ALLOWED_PROOF_EXTENSIONS = {'png', 'jpg', 'jpeg', 'pdf', 'gif', 'webp'}


def sanitize_tenant_name(name: str) -> str:
    """Convert tenant name to safe filename: lowercase, replace spaces with underscores."""
    if not name:
        return "tenant"
    # Remove special characters, keep only alphanumeric and spaces/underscores
    safe = re.sub(r'[^a-zA-Z0-9\s_]', '', name)
    return safe.strip().replace(' ', '_').lower()


def get_photo_filename(tenant_name: str) -> str:
    """Generate photo filename: photo_<sanitized_name>.jpg"""
    safe_name = sanitize_tenant_name(tenant_name)
    return f"photo_{safe_name}.jpg"


def get_proof_filename(tenant_name: str) -> str:
    """Generate proof filename: proof_<sanitized_name>.pdf"""
    safe_name = sanitize_tenant_name(tenant_name)
    return f"proof_{safe_name}.pdf"


def save_uploaded_file(file, folder: str, filename: str) -> str:
    """
    Save uploaded file to the specified folder.
    Returns the relative path for DB storage (e.g., 'uploads/photo_john.jpg').
    """
    if not file or not file.filename:
        return None
    
    # Ensure folder exists
    os.makedirs(folder, exist_ok=True)
    
    # Get file extension from original filename
    ext = os.path.splitext(file.filename)[1].lower()
    if not ext:
        ext = '.jpg'  # default
    
    # Create full path
    full_path = os.path.join(folder, filename + ext)
    
    # Save the file
    file.save(full_path)
    
    # Return relative path for DB
    return f"uploads/{filename}{ext}"


def is_allowed_photo(filename: str) -> bool:
    """Check if file is an allowed photo type."""
    ext = os.path.splitext(filename)[1].lower().lstrip('.')
    return ext in ALLOWED_PHOTO_EXTENSIONS


def is_allowed_proof(filename: str) -> bool:
    """Check if file is an allowed proof type (image or PDF)."""
    ext = os.path.splitext(filename)[1].lower().lstrip('.')
    return ext in ALLOWED_PROOF_EXTENSIONS


def check_verification_status(address: str, photo: str, proof_id: str) -> bool:
    """
    Auto-check verification based on required fields.
    Returns True if all required documents are present.
    """
    has_address = address and address.strip()
    has_photo = photo and photo.strip()
    has_proof = proof_id and proof_id.strip()
    
    return bool(has_address and has_photo and has_proof)
