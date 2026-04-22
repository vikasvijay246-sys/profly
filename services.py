"""
Business logic service layer.
All heavy operations go here — routes stay thin.
"""
import uuid
from datetime import datetime, timezone, timedelta, date
from calendar import monthrange
from models import (db, User, Property, PropertyTenant, Payment,
                    Room, RoomTenant, Notification, now_utc)


# ── Rent month helpers ────────────────────────────────────────────────────────
def fmt_month(dt=None):
    """Return 'YYYY-MM' for given datetime (or now)."""
    d = dt or datetime.now(timezone.utc)
    return d.strftime("%Y-%m")

def month_due_date(year, month):
    """1st of the given month as a naive datetime (UTC)."""
    return datetime(year, month, 1, 0, 0, 0)

def next_month(year, month):
    if month == 12:
        return year + 1, 1
    return year, month + 1

def parse_month(s):
    """'2025-06' → (2025, 6)"""
    y, m = s.split("-")
    return int(y), int(m)


# ── Monthly rent generation ────────────────────────────────────────────────────
def generate_monthly_rent(property_id=None, owner_id=None, force_month=None):
    """
    Create Payment records for all active tenants for the target month.
    Skips if a record for that month already exists (idempotent).
    Returns (created_count, skipped_count, errors).
    """
    target_month = force_month or fmt_month()
    year, month  = parse_month(target_month)
    due          = month_due_date(year, month)

    # Query active tenancies
    q = PropertyTenant.query.filter_by(status="active")
    if property_id:
        q = q.filter_by(property_id=property_id)
    if owner_id:
        # filter by owner's properties
        prop_ids = [p.id for p in
                    Property.query.filter_by(owner_id=owner_id, is_deleted=False).all()]
        if not prop_ids:
            return 0, 0, []
        q = q.filter(PropertyTenant.property_id.in_(prop_ids))

    tenancies = q.all()
    created = skipped = 0
    errors  = []

    for t in tenancies:
        try:
            # Check for duplicate
            exists = Payment.query.filter_by(
                tenant_id=t.tenant_id,
                property_id=t.property_id,
                rent_month=target_month,
                payment_type="rent",
            ).first()
            if exists:
                skipped += 1
                continue

            amt = float(t.property.monthly_rent) if t.property else 0
            pay = Payment(
                tenant_id    = t.tenant_id,
                property_id  = t.property_id,
                amount       = amt,
                payment_type = "rent",
                status       = "pending",
                rent_month   = target_month,
                due_date     = due,
                description  = f"Monthly rent — {target_month}",
            )
            db.session.add(pay)

            # Notify tenant
            notif = Notification(
                user_id    = t.tenant_id,
                title      = "Rent Due",
                body       = f"Your rent of ₹{amt:,.0f} for {target_month} is due on {due.strftime('%d %b %Y')}.",
                notif_type = "payment_due",
            )
            db.session.add(notif)
            created += 1

        except Exception as e:
            errors.append(str(e))

    db.session.commit()
    return created, skipped, errors


# ── Mark overdue payments ──────────────────────────────────────────────────────
def mark_overdue_payments():
    """
    Mark all pending payments whose due_date is in the past as overdue.
    Call this daily (or on dashboard load).
    Returns count of payments updated.
    """
    now   = now_utc()
    count = Payment.query.filter(
        Payment.status  == "pending",
        Payment.due_date < now,
    ).update({"status": "overdue"})
    db.session.commit()
    return count


# ── Payment history for a tenant ───────────────────────────────────────────────
def tenant_payment_history(tenant_id, months=12):
    """
    Returns last N months of rent records for a tenant, newest first.
    """
    return (Payment.query
            .filter_by(tenant_id=tenant_id, payment_type="rent")
            .order_by(Payment.rent_month.desc(), Payment.created_at.desc())
            .limit(months)
            .all())


# ── Owner payment dashboard summary ───────────────────────────────────────────
def owner_payment_summary(owner_id, target_month=None):
    """
    For a given month (default = current), return per-tenant payment status.
    """
    month = target_month or fmt_month()
    prop_ids = [p.id for p in
                Property.query.filter_by(owner_id=owner_id, is_deleted=False).all()]
    if not prop_ids:
        return []

    tenancies = (PropertyTenant.query
                 .filter(PropertyTenant.property_id.in_(prop_ids),
                         PropertyTenant.status == "active")
                 .all())

    summary = []
    for t in tenancies:
        pay = Payment.query.filter_by(
            tenant_id=t.tenant_id,
            property_id=t.property_id,
            rent_month=month,
            payment_type="rent",
        ).first()
        summary.append({
            "tenant_id":    t.tenant_id,
            "tenant_name":  t.tenant.full_name if t.tenant else "?",
            "tenant_phone": t.tenant.phone if t.tenant else "",
            "property_name": t.property.name if t.property else "?",
            "room_number":  t.room_number or "—",
            "amount":       float(t.property.monthly_rent) if t.property else 0,
            "status":       pay.status if pay else "no_record",
            "is_paid":      (pay.status == "completed") if pay else False,
            "paid_at":      pay.paid_at.strftime("%d-%m-%Y") if (pay and pay.paid_at) else None,
            "payment_id":   pay.id if pay else None,
        })

    return summary


# ── Assign tenant to room (with capacity validation) ──────────────────────────
def assign_tenant_to_room(room_id, tenant_id, current_user_id):
    """
    Safely assign a tenant to a room. Enforces max_capacity ≤ 4.
    Returns (ok: bool, message: str, room_tenant or None).
    """
    room   = Room.query.get(room_id)
    tenant = User.query.get(tenant_id)

    if not room:
        return False, "Room not found.", None
    if not tenant or tenant.role != "tenant":
        return False, "Tenant not found.", None

    occ = room.get_occupancy()
    if occ >= room.max_capacity:
        return False, f"Room {room.room_number} is full ({occ}/{room.max_capacity}).", None
    if occ >= 4:
        return False, "Maximum 4 tenants allowed per room.", None

    # Already assigned?
    existing = RoomTenant.query.filter_by(room_id=room_id, tenant_id=tenant_id).first()
    if existing:
        if existing.is_active:
            return False, "Tenant already assigned to this room.", None
        # Re-activate
        existing.is_active = True
        existing.vacated_at = None
        db.session.commit()
        return True, "Tenant re-assigned.", existing

    rt = RoomTenant(room_id=room_id, tenant_id=tenant_id,
                    payment_status="not_paid", is_active=True)
    db.session.add(rt)

    # Keep PropertyTenant.room_number in sync
    pt = PropertyTenant.query.filter_by(
        tenant_id=tenant_id, property_id=room.property_id
    ).first()
    if pt:
        pt.room_id     = room_id
        pt.room_number = str(room.room_number)

    db.session.commit()
    return True, f"Assigned to Room {room.room_number}.", rt


# ── Send push notification via SocketIO ────────────────────────────────────────
def push_notification(socketio, user_id, title, body, notif_type="general"):
    """Save DB notification + push real-time event."""
    notif = Notification(
        user_id=user_id, title=title, body=body, notif_type=notif_type
    )
    db.session.add(notif)
    db.session.commit()
    try:
        socketio.emit("notification", notif.to_dict(), room=f"user_{user_id}")
    except Exception:
        pass
    return notif
