"""
services package

Import from this package for backward compatibility:
    from services import fmt_month, generate_monthly_rent, ...

All new code should import from specific submodules:
    from services.payment import PaymentService
    from services.tenant  import TenantService
"""
# Helpers (backward compat with existing routes that import from services)
from services.helpers import (
    fmt_month,
    parse_month,
    month_due_date,
    next_month,
    current_rent_month,
)
from services.payment import PaymentService
from services.tenant  import TenantService
from services.message import MessageService
from services.room    import RoomService
from services.notification import NotificationService

# ── Function aliases for old routes that call functions directly ───────────────
_payment_svc = None
_tenant_svc  = None


def _get_payment_svc():
    global _payment_svc
    if _payment_svc is None:
        _payment_svc = PaymentService()
    return _payment_svc


def _get_tenant_svc():
    global _tenant_svc
    if _tenant_svc is None:
        _tenant_svc = TenantService()
    return _tenant_svc


def generate_monthly_rent(property_id=None, owner_id=None, force_month=None):
    return _get_payment_svc().generate_monthly_rent(
        property_id=property_id,
        owner_id=owner_id,
        force_month=force_month,
    )


def mark_overdue_payments():
    return _get_payment_svc().mark_overdue()


def tenant_payment_history(tenant_id, months=12):
    return _get_payment_svc().history(tenant_id, months=months)


def owner_payment_summary(owner_id, target_month=None):
    return _get_payment_svc().owner_summary(owner_id, target_month=target_month)


def assign_tenant_to_room(room_id, tenant_id, current_user_id):
    return RoomService().assign_tenant(room_id, tenant_id, current_user_id)


def push_notification(socketio, user_id, title, body, notif_type="general"):
    return NotificationService().push(socketio, user_id, title, body, notif_type)
