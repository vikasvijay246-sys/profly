# Migration Guide — PropFlow Upgrade

## SQLite (local dev)
```bash
rm propflow.db   # drops old DB
python app.py    # recreates all tables + seeds fresh data
```

## PostgreSQL (production — safe, additive only)

Run these in order. All statements are safe to re-run.

```sql
-- 1. Add rent_month to payments
ALTER TABLE payments ADD COLUMN IF NOT EXISTS rent_month VARCHAR(7);
CREATE INDEX IF NOT EXISTS ix_payments_rent_month ON payments (rent_month);

-- 2. Add unique constraint (prevents duplicate monthly records)
-- Only run if not already present:
ALTER TABLE payments ADD CONSTRAINT uq_payment_tenant_property_month
  UNIQUE (tenant_id, property_id, rent_month);

-- 3. Add room_id + room_number to property_tenants
ALTER TABLE property_tenants ADD COLUMN IF NOT EXISTS room_id INTEGER REFERENCES rooms(id) ON DELETE SET NULL;
ALTER TABLE property_tenants ADD COLUMN IF NOT EXISTS room_number VARCHAR(20);

-- 4. Add file_size and is_deleted to messages
ALTER TABLE messages ADD COLUMN IF NOT EXISTS file_size INTEGER;
ALTER TABLE messages ADD COLUMN IF NOT EXISTS is_deleted BOOLEAN DEFAULT FALSE NOT NULL;
CREATE INDEX IF NOT EXISTS ix_msg_room_created ON messages (room_id, created_at);
CREATE INDEX IF NOT EXISTS ix_msg_receiver_unread ON messages (receiver_id, is_read);

-- 5. Add new columns to rooms
ALTER TABLE rooms ADD COLUMN IF NOT EXISTS floor VARCHAR(20);
ALTER TABLE rooms ADD COLUMN IF NOT EXISTS amenities VARCHAR(500);
ALTER TABLE rooms ADD COLUMN IF NOT EXISTS is_active BOOLEAN DEFAULT TRUE NOT NULL;
ALTER TABLE rooms ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP;

-- 6. Add is_active + vacated_at to room_tenants
ALTER TABLE room_tenants ADD COLUMN IF NOT EXISTS is_active BOOLEAN DEFAULT TRUE NOT NULL;
ALTER TABLE room_tenants ADD COLUMN IF NOT EXISTS vacated_at TIMESTAMP;

-- 7. Room capacity constraint (1–4) — PostgreSQL only
ALTER TABLE rooms ADD CONSTRAINT ck_room_capacity
  CHECK (max_capacity >= 1 AND max_capacity <= 4);

-- 8. Composite indexes for performance
CREATE INDEX IF NOT EXISTS ix_users_owner_role      ON users (owner_id, role);
CREATE INDEX IF NOT EXISTS ix_payments_tenant_status ON payments (tenant_id, status);
CREATE INDEX IF NOT EXISTS ix_payments_due_date      ON payments (due_date);
CREATE INDEX IF NOT EXISTS ix_notif_user_read        ON notifications (user_id, is_read);
CREATE INDEX IF NOT EXISTS ix_rooms_owner_id         ON rooms (owner_id);
CREATE INDEX IF NOT EXISTS ix_pt_tenant_id           ON property_tenants (tenant_id);
CREATE INDEX IF NOT EXISTS ix_pt_property_id         ON property_tenants (property_id);
CREATE INDEX IF NOT EXISTS ix_rt_tenant_id           ON room_tenants (tenant_id);
CREATE INDEX IF NOT EXISTS ix_rt_room_id             ON room_tenants (room_id);
```

## Environment variables (.env)
```
DATABASE_URL=postgresql://user:password@host:5432/propflow
SECRET_KEY=<random 64-char string>
MAX_CONTENT_LENGTH_MB=10
RENT_GENERATE_DAY=25
ADMIN_PHONE=admin
ADMIN_PASSWORD=<strong password>
```
