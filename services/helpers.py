"""
Pure utility functions used across service classes.
No DB access, no Flask context required.
"""
from datetime import datetime, timezone


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
