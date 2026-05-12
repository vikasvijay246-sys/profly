"""
SQLite schema upgrades for additive columns (no Alembic).
Runs safely at startup; skips columns that already exist.
"""
from sqlalchemy import text


def _sqlite_columns(conn, table: str) -> set:
    rows = conn.execute(text(f"PRAGMA table_info({table})")).fetchall()
    return {r[1] for r in rows}


def upgrade_sqlite_schema(db):
    """Add missing columns/indexes for lightweight migrations."""
    bind = db.engine
    if bind.dialect.name != "sqlite":
        return

    with bind.connect() as conn:
        cols = _sqlite_columns(conn, "users")
        if "tenant_public_id" not in cols:
            conn.execute(text("ALTER TABLE users ADD COLUMN tenant_public_id VARCHAR(48)"))
        if "designation" not in cols:
            conn.execute(text("ALTER TABLE users ADD COLUMN designation VARCHAR(40)"))

        pcols = _sqlite_columns(conn, "properties")
        if "short_code" not in pcols:
            conn.execute(text("ALTER TABLE properties ADD COLUMN short_code VARCHAR(16)"))

        mcols = _sqlite_columns(conn, "messages")
        if "property_id" not in mcols:
            conn.execute(text("ALTER TABLE messages ADD COLUMN property_id INTEGER"))

        conn.commit()

    # Unique partial index for tenant IDs (SQLite)
    with bind.connect() as conn:
        conn.execute(text(
            "CREATE UNIQUE INDEX IF NOT EXISTS ix_users_tenant_public_id_unique "
            "ON users(tenant_public_id) WHERE tenant_public_id IS NOT NULL"
        ))
        conn.commit()
