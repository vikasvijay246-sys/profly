"""
PaymentService
--------------
All payment lifecycle logic: create, complete, overdue-mark, monthly generation.

SAFETY RULES:
  1. Validate tenant_id and property_id exist before inserting
  2. Prevent duplicate monthly records (idempotent generation)
  3. transaction() wraps every multi-step write
  4. Rollback on ANY failure — no partial records
  5. Audit log on every state transition
"""
import uuid
from decimal import Decimal
from datetime import datetime

from models import (db, User, Property, PropertyTenant,
                    Payment, Notification, now_utc)
from services.base import BaseService
from services.helpers import fmt_month, parse_month, month_due_date
from utils.errors import (
    ValidationError, NotFoundError, ConflictError, PermissionError_
)


class PaymentService(BaseService):

    VALID_STATUSES = {"pending", "completed", "overdue", "failed", "waived"}

    # ── Create payment record ──────────────────────────────────────────────────
    def create(self, data: dict, created_by_id: int) -> Payment:
        """
        Create a single payment record.
        `data` must be pre-validated via validate_create_payment().
        Sends a notification to the tenant.
        """
        tenant_id   = data["tenant_id"]
        property_id = data["property_id"]
        amount      = Decimal(str(data["amount"]))
        rent_month  = data.get("rent_month")

        # Guard: tenant must exist and be active
        tenant = User.query.filter_by(id=tenant_id, role="tenant", is_active=True).first()
        if not tenant:
            raise NotFoundError("Active tenant", tenant_id)

        # Guard: property must exist and not be deleted
        prop = Property.query.filter_by(id=property_id, is_deleted=False).first()
        if not prop:
            raise NotFoundError("Property", property_id)

        # Guard: if rent_month given, prevent duplicates
        if rent_month:
            dup = Payment.query.filter_by(
                tenant_id   = tenant_id,
                property_id = property_id,
                rent_month  = rent_month,
                payment_type= data.get("payment_type", "rent"),
            ).first()
            if dup:
                raise ConflictError(
                    f"Payment for {rent_month} already exists (id={dup.id})",
                    payment_id=dup.id, rent_month=rent_month,
                )

        pay = Payment(
            tenant_id      = tenant_id,
            property_id    = property_id,
            amount         = amount,
            payment_type   = data.get("payment_type", "rent"),
            status         = "pending",
            rent_month     = rent_month,
            due_date       = data.get("due_date"),
            description    = data.get("description", ""),
        )

        with self.transaction(f"create_payment t={tenant_id} p={property_id}"):
            db.session.add(pay)
            db.session.flush()   # get pay.id

            # Notify tenant inline (same transaction — all or nothing)
            notif = Notification(
                user_id    = tenant_id,
                title      = "Payment Due",
                body       = f"A payment of ₹{amount:,.0f} ({pay.payment_type}) is due."
                             + (f" Month: {rent_month}" if rent_month else ""),
                notif_type = "payment_due",
            )
            db.session.add(notif)

        self.log.info(
            "Payment created",
            extra={
                "payment_id": pay.id, "tenant_id": tenant_id,
                "property_id": property_id, "amount": float(amount),
                "rent_month": rent_month, "created_by": created_by_id,
            },
        )
        return pay

    # ── Complete (mark as paid) ────────────────────────────────────────────────
    def complete(
        self,
        payment_id:     int,
        tenant_id:      int,
        payment_method: str = "online",
        notes:          str = "",
    ) -> Payment:
        """
        Mark a payment as completed.
        Called by the tenant themselves OR by owner/admin on their behalf.
        """
        pay = Payment.query.filter_by(id=payment_id).first()
        if not pay:
            raise NotFoundError("Payment", payment_id)

        # Ownership check: tenant_id must match OR be admin
        if pay.tenant_id != tenant_id:
            caller = User.query.get(tenant_id)
            if not caller or caller.role not in ("admin", "owner"):
                raise PermissionError_(
                    f"Payment {payment_id} does not belong to tenant {tenant_id}"
                )

        if pay.status == "completed":
            raise ConflictError(
                f"Payment {payment_id} is already marked as completed",
                payment_id=payment_id,
            )
        if pay.status == "waived":
            raise ConflictError(
                f"Payment {payment_id} has been waived and cannot be marked paid",
                payment_id=payment_id,
            )

        with self.transaction(f"complete_payment id={payment_id}"):
            pay.status         = "completed"
            pay.paid_at        = now_utc()
            pay.payment_method = payment_method or "online"
            pay.transaction_id = f"TXN-{uuid.uuid4().hex[:10].upper()}"
            if notes:
                pay.notes = str(notes)[:500]

            # Notify
            notif = Notification(
                user_id    = pay.tenant_id,
                title      = "✅ Payment Successful",
                body       = (f"₹{pay.amount:,.0f} paid for "
                              f"{pay.rent_month or pay.payment_type}. "
                              f"Ref: {pay.transaction_id}"),
                notif_type = "payment_received",
            )
            db.session.add(notif)

        self.log.info(
            "Payment completed",
            extra={
                "payment_id": payment_id, "tenant_id": pay.tenant_id,
                "amount": float(pay.amount), "txn_id": pay.transaction_id,
            },
        )
        return pay

    # ── Update status (owner/admin) ────────────────────────────────────────────
    def update_status(self, payment_id: int, new_status: str, updater_id: int) -> Payment:
        if new_status not in self.VALID_STATUSES:
            raise ValidationError(
                f"Invalid status '{new_status}'. "
                f"Valid: {', '.join(sorted(self.VALID_STATUSES))}"
            )
        pay = Payment.query.get(payment_id)
        if not pay:
            raise NotFoundError("Payment", payment_id)

        old_status  = pay.status
        pay.status  = new_status
        if new_status == "completed" and not pay.paid_at:
            pay.paid_at        = now_utc()
            pay.transaction_id = pay.transaction_id or f"TXN-{uuid.uuid4().hex[:10].upper()}"

        self.safe_commit(f"update_payment_status id={payment_id}")

        self.log.info(
            "Payment status updated",
            extra={
                "payment_id": payment_id, "old_status": old_status,
                "new_status": new_status, "updater_id": updater_id,
            },
        )
        return pay

    # ── Monthly rent generation ────────────────────────────────────────────────
    def generate_monthly_rent(
        self,
        property_id: int = None,
        owner_id:    int = None,
        force_month: str = None,
    ) -> tuple:
        """
        Idempotent: create pending rent records for all active tenants.
        Returns (created, skipped, errors[]).

        SAFETY: each tenant record is its own inner transaction.
        A failure for one tenant does NOT roll back others.
        """
        target_month = force_month or fmt_month()
        try:
            year, month = parse_month(target_month)
        except ValueError as e:
            raise ValidationError(str(e))

        due = month_due_date(year, month)

        # Scope query
        q = PropertyTenant.query.filter_by(status="active")
        if property_id:
            q = q.filter_by(property_id=property_id)
        if owner_id:
            prop_ids = [
                p.id for p in
                Property.query.filter_by(owner_id=owner_id, is_deleted=False).all()
            ]
            if not prop_ids:
                return 0, 0, []
            q = q.filter(PropertyTenant.property_id.in_(prop_ids))

        tenancies = q.all()
        created = skipped = 0
        errors  = []

        for t in tenancies:
            # Defensive: skip if FK is broken
            if not t.tenant_id or not t.property_id:
                errors.append(f"PropertyTenant id={t.id}: missing tenant_id or property_id — skipped")
                self.log.error(
                    "Corrupt PropertyTenant skipped",
                    extra={"pt_id": t.id, "tenant_id": t.tenant_id,
                           "property_id": t.property_id},
                )
                continue

            # Defensive: verify tenant still active
            if not t.tenant or not t.tenant.is_active:
                skipped += 1
                continue

            # Defensive: verify property still exists
            if not t.property or t.property.is_deleted:
                errors.append(f"Tenant {t.tenant_id}: property {t.property_id} deleted — skipped")
                continue

            try:
                # Idempotency check
                exists = Payment.query.filter_by(
                    tenant_id   = t.tenant_id,
                    property_id = t.property_id,
                    rent_month  = target_month,
                    payment_type= "rent",
                ).first()
                if exists:
                    skipped += 1
                    continue

                amt = float(t.property.monthly_rent)

                # Each record is its own transaction — one failure ≠ all fail
                with self.transaction(
                    f"gen_rent t={t.tenant_id} p={t.property_id} m={target_month}"
                ):
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
                    db.session.flush()

                    notif = Notification(
                        user_id    = t.tenant_id,
                        title      = "🏠 Rent Due",
                        body       = (f"Rent of ₹{amt:,.0f} for {target_month} "
                                      f"is due on {due.strftime('%d %b %Y')}."),
                        notif_type = "payment_due",
                    )
                    db.session.add(notif)

                created += 1

            except ConflictError:
                skipped += 1
            except Exception as exc:
                errors.append(f"Tenant {t.tenant_id}: {exc}")
                self.log.error(
                    "Rent generation failed for tenant",
                    extra={"tenant_id": t.tenant_id, "reason": str(exc)},
                )

        self.log.info(
            "Monthly rent generated",
            extra={"month": target_month, "created_count": created,
                   "skipped": skipped, "errors": len(errors)},
        )
        return created, skipped, errors

    # ── Mark overdue ───────────────────────────────────────────────────────────
    def mark_overdue(self) -> int:
        """
        Bulk-update pending payments past their due_date to 'overdue'.
        Single SQL UPDATE — no loop, no N+1.
        """
        now = now_utc()
        count = (
            Payment.query
            .filter(Payment.status == "pending", Payment.due_date < now)
            .update({"status": "overdue"}, synchronize_session="fetch")
        )
        self.safe_commit("mark_overdue_payments")

        if count:
            self.log.info("Payments marked overdue", extra={"count": count})
        return count

    # ── History ────────────────────────────────────────────────────────────────
    def history(self, tenant_id: int, months: int = 12):
        return (
            Payment.query
            .filter_by(tenant_id=tenant_id, payment_type="rent")
            .order_by(Payment.rent_month.desc(), Payment.created_at.desc())
            .limit(months)
            .all()
        )

    # ── Owner monthly summary ──────────────────────────────────────────────────
    def owner_summary(self, owner_id: int, target_month: str = None) -> list:
        """
        Return per-tenant paid/unpaid summary for a given month.
        Uses a single JOIN query — no N+1.
        """
        month = target_month or fmt_month()

        prop_ids = [
            p.id for p in
            Property.query.filter_by(owner_id=owner_id, is_deleted=False).all()
        ]
        if not prop_ids:
            return []

        # Single query: tenancies + their payment for the month (LEFT JOIN)
        from sqlalchemy.orm import aliased
        from sqlalchemy import and_, outerjoin

        tenancies = (
            PropertyTenant.query
            .filter(
                PropertyTenant.property_id.in_(prop_ids),
                PropertyTenant.status == "active",
            )
            .all()
        )

        # Batch-load payments for this month in ONE query to avoid N+1
        payment_map: dict = {}
        if tenancies:
            t_ids = [t.tenant_id   for t in tenancies]
            p_ids = [t.property_id for t in tenancies]
            pays  = (
                Payment.query
                .filter(
                    Payment.tenant_id.in_(t_ids),
                    Payment.property_id.in_(p_ids),
                    Payment.rent_month  == month,
                    Payment.payment_type == "rent",
                )
                .all()
            )
            for pay in pays:
                payment_map[(pay.tenant_id, pay.property_id)] = pay

        summary = []
        for t in tenancies:
            # Safe: check FK objects before accessing
            tenant_obj  = t.tenant
            property_obj = t.property

            if not tenant_obj or not tenant_obj.is_active:
                continue   # skip orphaned tenancies
            if not property_obj or property_obj.is_deleted:
                continue

            pay = payment_map.get((t.tenant_id, t.property_id))
            summary.append({
                "tenant_id":     t.tenant_id,
                "tenant_name":   tenant_obj.full_name,
                "tenant_phone":  tenant_obj.phone,
                "property_name": property_obj.name,
                "room_number":   t.room_number or "—",
                "amount":        float(property_obj.monthly_rent),
                "status":        pay.status if pay else "no_record",
                "is_paid":       (pay.status == "completed") if pay else False,
                "paid_at":       (pay.paid_at.strftime("%d-%m-%Y")
                                  if pay and pay.paid_at else None),
                "payment_id":    pay.id if pay else None,
            })

        return summary

    # ── Safe delete payment ────────────────────────────────────────────────────
    def delete(self, payment_id: int, deleted_by_id: int) -> bool:
        """
        Hard-delete a payment record.
        Only allowed by admin OR the owner of the property.
        Logs audit trail before deletion.
        """
        pay = Payment.query.get(payment_id)
        if not pay:
            raise NotFoundError("Payment", payment_id)

        caller = User.query.get(deleted_by_id)
        if not caller:
            raise NotFoundError("Caller", deleted_by_id)

        if caller.role == "tenant":
            raise PermissionError_("Tenants cannot delete payment records")

        # Ownership check for owners
        if caller.role == "owner":
            prop = Property.query.get(pay.property_id)
            if not prop or prop.owner_id != deleted_by_id:
                raise PermissionError_(
                    f"Owner {deleted_by_id} does not own property {pay.property_id}"
                )

        self.log.warning(
            "Payment deleted",
            extra={
                "payment_id": payment_id, "amount": float(pay.amount),
                "status": pay.status, "tenant_id": pay.tenant_id,
                "deleted_by": deleted_by_id,
            },
        )

        with self.transaction(f"delete_payment id={payment_id}"):
            db.session.delete(pay)

        return True
