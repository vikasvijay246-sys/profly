"""
Owner routes — thin layer: validate input → call service → respond.
No business logic, no direct DB queries (except simple reads in dashboard).
"""
from flask import Blueprint, render_template, request, redirect, url_for, flash, jsonify
from flask_login import login_required, current_user
from models import db, User, Property, PropertyTenant, Payment, Room, now_utc
from routes import role_required
from services import (generate_monthly_rent, mark_overdue_payments,
                      owner_payment_summary, push_notification, fmt_month,
                      TenantService, PaymentService)
from utils.validators import (validate_create_tenant, validate_create_payment,
                               optional_date, optional_id, require_amount,
                               require_payment_type, optional_rent_month,
                               optional_string, require_payment_status)
from utils.errors import AppError, ValidationError

owner_bp = Blueprint("owner", __name__, url_prefix="/owner")
_tenant_svc  = TenantService()
_payment_svc = PaymentService()


@owner_bp.route("/dashboard")
@login_required
@role_required("owner")
def dashboard():
    mark_overdue_payments()

    props  = Property.query.filter_by(owner_id=current_user.id, is_deleted=False).all()
    p_ids  = [p.id for p in props]
    rooms  = Room.query.filter_by(owner_id=current_user.id, is_active=True).all()
    t_q    = User.query.filter_by(owner_id=current_user.id, role="tenant")

    # Batch occupancy — avoid N+1
    from sqlalchemy import func
    from models import RoomTenant
    occ_map = {}
    if rooms:
        rows = (db.session.query(RoomTenant.room_id, func.count(RoomTenant.id))
                .filter(RoomTenant.is_active == True)
                .filter(RoomTenant.room_id.in_([r.id for r in rooms]))
                .group_by(RoomTenant.room_id).all())
        occ_map = {rid: cnt for rid, cnt in rows}

    stats = {
        "total_properties": len(props),
        "occupied":         sum(1 for p in props if p.status == "occupied"),
        "available":        sum(1 for p in props if p.status == "available"),
        "total_tenants":    t_q.count(),
        "total_rooms":      len(rooms),
        "occupied_rooms":   sum(1 for r in rooms if occ_map.get(r.id, 0) > 0),
        "vacant_rooms":     sum(1 for r in rooms if occ_map.get(r.id, 0) == 0),
        "revenue": float(
            db.session.query(db.func.coalesce(db.func.sum(Payment.amount), 0))
            .filter(Payment.property_id.in_(p_ids), Payment.status == "completed")
            .scalar() or 0
        ) if p_ids else 0.0,
        "pending_payments": Payment.query.filter(
            Payment.property_id.in_(p_ids), Payment.status == "pending"
        ).count() if p_ids else 0,
        "overdue_payments": Payment.query.filter(
            Payment.property_id.in_(p_ids), Payment.status == "overdue"
        ).count() if p_ids else 0,
    }

    room_data = []
    for r in rooms:
        from models import RoomTenant as RT
        assignments = RT.query.filter_by(room_id=r.id, is_active=True).all()
        paid_cnt    = sum(1 for a in assignments if a.payment_status == "paid")
        room_data.append({
            "room": r, "assignments": assignments,
            "occupancy": len(assignments),
            "vacant":    max(0, r.max_capacity - len(assignments)),
            "paid_count": paid_cnt,
        })

    target_month = request.args.get("month", fmt_month())
    pay_summary  = _payment_svc.owner_summary(current_user.id, target_month)

    recent_payments = (
        Payment.query.filter(Payment.property_id.in_(p_ids))
        .order_by(Payment.created_at.desc()).limit(10).all()
    ) if p_ids else []

    return render_template("owner/dashboard.html",
                           stats=stats, recent_payments=recent_payments,
                           props=props, room_data=room_data,
                           pay_summary=pay_summary, target_month=target_month)


@owner_bp.route("/generate-rent", methods=["POST"])
@login_required
@role_required("owner")
def generate_rent():
    month = request.form.get("month", fmt_month())
    try:
        created, skipped, errors = _payment_svc.generate_monthly_rent(
            owner_id=current_user.id, force_month=month
        )
        if errors:
            flash(f"Partial errors: {'; '.join(errors[:3])}", "error")
        flash(f"Rent for {month}: {created} created, {skipped} already existed.", "success")
    except AppError as e:
        flash(str(e), "error")
    return redirect(url_for("owner.dashboard"))


# ── Properties ────────────────────────────────────────────────────────────────
@owner_bp.route("/properties")
@login_required
@role_required("owner")
def properties():
    props = Property.query.filter_by(owner_id=current_user.id, is_deleted=False).all()
    return render_template("owner/properties.html", properties=props)


@owner_bp.route("/properties/add", methods=["POST"])
@login_required
@role_required("owner")
def add_property():
    try:
        from utils.validators import require_string, require_amount, optional_string
        name    = require_string(request.form.get("name"),    "name", max_len=200)
        address = require_string(request.form.get("address"), "address")
        city    = require_string(request.form.get("city"),    "city", max_len=100)
        rent    = require_amount(request.form.get("monthly_rent"), "monthly_rent")
    except ValidationError as e:
        flash(str(e), "error")
        return redirect(url_for("owner.properties"))

    prop = Property(
        owner_id=current_user.id,
        name=name, address=address, city=city,
        state=request.form.get("state","").strip(),
        zip_code=request.form.get("zip_code","").strip(),
        unit_number=request.form.get("unit_number","").strip(),
        property_type=request.form.get("property_type","apartment"),
        bedrooms=int(request.form["bedrooms"]) if request.form.get("bedrooms") else None,
        bathrooms=int(request.form["bathrooms"]) if request.form.get("bathrooms") else None,
        area_sqft=float(request.form["area_sqft"]) if request.form.get("area_sqft") else None,
        monthly_rent=rent,
        description=request.form.get("description",""),
        status="available",
    )
    db.session.add(prop)
    try:
        db.session.commit()
        flash("Property added.", "success")
    except Exception:
        db.session.rollback()
        flash("Failed to save property. Please retry.", "error")
    return redirect(url_for("owner.properties"))


@owner_bp.route("/properties/<int:pid>/edit", methods=["POST"])
@login_required
@role_required("owner")
def edit_property(pid):
    prop = Property.query.filter_by(id=pid, owner_id=current_user.id).first_or_404()
    try:
        if request.form.get("monthly_rent"):
            prop.monthly_rent = require_amount(request.form["monthly_rent"])
    except ValidationError as e:
        flash(str(e), "error")
        return redirect(url_for("owner.properties"))

    prop.name   = request.form.get("name",   prop.name).strip()
    prop.address= request.form.get("address",prop.address).strip()
    prop.city   = request.form.get("city",   prop.city or "").strip()
    prop.state  = request.form.get("state",  prop.state or "").strip()
    prop.status = request.form.get("status", prop.status)
    try:
        db.session.commit()
        flash("Property updated.", "success")
    except Exception:
        db.session.rollback()
        flash("Update failed.", "error")
    return redirect(url_for("owner.properties"))


@owner_bp.route("/properties/<int:pid>/delete", methods=["POST"])
@login_required
@role_required("owner")
def delete_property(pid):
    prop = Property.query.filter_by(id=pid, owner_id=current_user.id).first_or_404()
    prop.is_deleted = True
    try:
        db.session.commit()
        flash("Property removed.", "success")
    except Exception:
        db.session.rollback()
        flash("Delete failed.", "error")
    return redirect(url_for("owner.properties"))


# ── Tenants ────────────────────────────────────────────────────────────────────
@owner_bp.route("/tenants")
@login_required
@role_required("owner")
def tenants():
    my_tenants = _tenant_svc.list_for_owner(current_user.id, include_inactive=True)
    my_props   = Property.query.filter_by(owner_id=current_user.id, is_deleted=False).all()
    return render_template("owner/tenants.html", tenants=my_tenants, properties=my_props)


@owner_bp.route("/tenants/add", methods=["POST"])
@login_required
@role_required("owner")
def add_tenant():
    try:
        data   = validate_create_tenant(request.form)
        tenant = _tenant_svc.create(data, owner_id=current_user.id)

        # Optional property assignment
        prop_id = optional_id(request.form.get("property_id"), "property_id")
        if prop_id:
            ls = optional_date(request.form.get("lease_start"), "lease_start")
            le = optional_date(request.form.get("lease_end"),   "lease_end")
            dep = optional_amount(request.form.get("deposit"), "deposit") if request.form.get("deposit") else None
            try:
                _tenant_svc.assign_to_property(
                    tenant.id, prop_id, current_user.id,
                    lease_start=ls, lease_end=le, deposit_amount=dep,
                )
            except AppError as e:
                flash(f"Tenant created but property assignment failed: {e}", "error")
                return redirect(url_for("owner.tenants"))

        # Welcome notification
        try:
            from app import socketio
            push_notification(socketio, tenant.id, "Welcome!",
                              f"Hello {tenant.full_name}, your account is ready. Login: {tenant.phone}",
                              "general")
        except Exception:
            pass

        flash(f"Tenant '{tenant.full_name}' created. Login: {tenant.phone}", "success")

    except AppError as e:
        flash(str(e), "error")

    return redirect(url_for("owner.tenants"))


@owner_bp.route("/tenants/<int:tid>/edit", methods=["POST"])
@login_required
@role_required("owner")
def edit_tenant(tid):
    try:
        _tenant_svc.update(tid, current_user.id, {
            "full_name": request.form.get("full_name"),
            "password":  request.form.get("password"),
            "is_active": request.form.get("is_active") == "1",
        })
        flash("Tenant updated.", "success")
    except AppError as e:
        flash(str(e), "error")
    return redirect(url_for("owner.tenants"))


@owner_bp.route("/tenants/<int:tid>/delete", methods=["POST"])
@login_required
@role_required("owner")
def delete_tenant(tid):
    """Soft-delete: preserves payment history, marks inactive."""
    try:
        _tenant_svc.deactivate(tid, current_user.id)
        flash("Tenant deactivated (history preserved).", "success")
    except AppError as e:
        flash(str(e), "error")
    return redirect(url_for("owner.tenants"))


@owner_bp.route("/tenants/<int:tid>/history")
@login_required
@role_required("owner")
def tenant_history(tid):
    tenant = User.query.filter_by(id=tid, owner_id=current_user.id, role="tenant").first_or_404()
    history = _payment_svc.history(tenant.id, months=24)
    return render_template("owner/tenant_history.html", tenant=tenant, history=history)


# ── Payments ──────────────────────────────────────────────────────────────────
@owner_bp.route("/payments")
@login_required
@role_required("owner")
def payments():
    prop_ids = [p.id for p in Property.query.filter_by(
        owner_id=current_user.id, is_deleted=False).all()]
    sf = request.args.get("status", "")
    mf = request.args.get("month",  "")
    q  = (Payment.query.filter(Payment.property_id.in_(prop_ids))
          if prop_ids else Payment.query.filter_by(id=-1))
    if sf: q = q.filter_by(status=sf)
    if mf: q = q.filter_by(rent_month=mf)
    all_payments = q.order_by(Payment.rent_month.desc(),
                               Payment.created_at.desc()).all()
    months = [r[0] for r in
              db.session.query(Payment.rent_month)
              .filter(Payment.property_id.in_(prop_ids),
                      Payment.rent_month.isnot(None))
              .distinct().order_by(Payment.rent_month.desc()).all()
              ] if prop_ids else []

    my_tenants = User.query.filter_by(owner_id=current_user.id, role="tenant").all()
    my_props   = Property.query.filter_by(owner_id=current_user.id, is_deleted=False).all()
    return render_template("owner/payments.html",
                           payments=all_payments, tenants=my_tenants,
                           properties=my_props, status_filter=sf,
                           month_filter=mf, available_months=months)


@owner_bp.route("/payments/add", methods=["POST"])
@login_required
@role_required("owner")
def add_payment():
    try:
        data = validate_create_payment(request.form)
        pay  = _payment_svc.create(data, created_by_id=current_user.id)
        # Push real-time notification
        try:
            from app import socketio
            push_notification(socketio, pay.tenant_id,
                              "New Payment Due",
                              f"₹{pay.amount:,.0f} ({pay.payment_type}) for {pay.rent_month or 'rent'} due.",
                              "payment_due")
        except Exception:
            pass
        flash("Payment created.", "success")
    except AppError as e:
        flash(str(e), "error")
    return redirect(url_for("owner.payments"))


@owner_bp.route("/payments/<int:pid>/update-status", methods=["POST"])
@login_required
@role_required("owner")
def update_payment(pid):
    try:
        status = require_payment_status(request.form.get("status"), "status")
        _payment_svc.update_status(pid, status, updater_id=current_user.id)
        flash("Payment updated.", "success")
    except AppError as e:
        flash(str(e), "error")
    return redirect(url_for("owner.payments"))


@owner_bp.route("/payments/<int:pid>/delete", methods=["POST"])
@login_required
@role_required("owner")
def delete_payment(pid):
    try:
        _payment_svc.delete(pid, deleted_by_id=current_user.id)
        flash("Payment deleted.", "success")
    except AppError as e:
        flash(str(e), "error")
    return redirect(url_for("owner.payments"))


# ── Notification ──────────────────────────────────────────────────────────────
@owner_bp.route("/notify", methods=["POST"])
@login_required
@role_required("owner")
def send_notification():
    from utils.validators import require_id, require_string
    try:
        tid   = require_id(request.form.get("tenant_id"), "tenant_id")
        title = require_string(request.form.get("title"), "title", max_len=255)
        body  = require_string(request.form.get("body"),  "body",  max_len=1000)
        try:
            from app import socketio
            push_notification(socketio, tid, title, body, "general")
        except Exception:
            push_notification(None, tid, title, body, "general")
        flash("Notification sent.", "success")
    except AppError as e:
        flash(str(e), "error")
    return redirect(url_for("owner.dashboard"))


# ── JSON API ──────────────────────────────────────────────────────────────────
@owner_bp.route("/api/stats")
@login_required
@role_required("owner")
def api_stats():
    props  = Property.query.filter_by(owner_id=current_user.id, is_deleted=False).all()
    p_ids  = [p.id for p in props]
    rooms  = Room.query.filter_by(owner_id=current_user.id, is_active=True).all()
    return jsonify({
        "ok": True,
        "data": {
            "total_rooms":    len(rooms),
            "occupied_rooms": sum(1 for r in rooms if r.get_occupancy() > 0),
            "vacant_rooms":   sum(1 for r in rooms if r.get_occupancy() == 0),
            "total_tenants":  User.query.filter_by(owner_id=current_user.id, role="tenant").count(),
            "pending_payments": Payment.query.filter(
                Payment.property_id.in_(p_ids), Payment.status == "pending"
            ).count() if p_ids else 0,
        }
    })
