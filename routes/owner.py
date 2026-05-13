"""
Owner routes — thin layer: validate input → call service → respond.
No business logic, no direct DB queries (except simple reads in dashboard).
"""
from flask import Blueprint, render_template, request, redirect, url_for, flash, jsonify
from flask_login import login_required, current_user
from models import db, User, Property, PropertyTenant, Payment, Room, RoomTenant, now_utc
from routes import role_required
from services import (generate_monthly_rent, mark_overdue_payments,
                      owner_payment_summary, push_notification, fmt_month,
                      TenantService, PaymentService)
from static.utils.validators import (validate_create_tenant, validate_create_payment,
                               optional_date, optional_id, require_amount,
                               require_payment_type, optional_rent_month,
                               optional_string, require_payment_status,optional_amount)
from utils.errors import AppError, ValidationError

owner_bp = Blueprint("owner", __name__, url_prefix="/owner")
_tenant_svc  = TenantService()
_payment_svc = PaymentService()


@owner_bp.route("/dashboard")
@login_required
@role_required("owner")
def dashboard():
    props = Property.query.filter_by(owner_id=current_user.id, is_deleted=False).all()
    if len(props) == 1:
        return redirect(url_for('owner.property_dashboard', property_id=props[0].id))
    else:
        return redirect(url_for('owner.property_selection'))


@owner_bp.route("/property/<int:property_id>/dashboard")
@login_required
@role_required("owner")
def property_dashboard(property_id):
    prop = Property.query.filter_by(id=property_id, owner_id=current_user.id, is_deleted=False).first_or_404()

    mark_overdue_payments()

    rooms = Room.query.filter_by(property_id=property_id, is_active=True).all()
    tenants = PropertyTenant.query.filter_by(property_id=property_id, status="active").all()

    # Batch occupancy
    from sqlalchemy import func
    occ_map = {}
    if rooms:
        rows = (db.session.query(RoomTenant.room_id, func.count(RoomTenant.id))
                .filter(RoomTenant.is_active == True)
                .filter(RoomTenant.room_id.in_([r.id for r in rooms]))
                .group_by(RoomTenant.room_id).all())
        occ_map = {rid: cnt for rid, cnt in rows}

    stats = {
        "total_tenants": len(tenants),
        "total_rooms": len(rooms),
        "occupied_rooms": sum(1 for r in rooms if occ_map.get(r.id, 0) > 0),
        "vacant_rooms": sum(1 for r in rooms if occ_map.get(r.id, 0) == 0),
        "revenue": float(
            db.session.query(db.func.coalesce(db.func.sum(Payment.amount), 0))
            .filter(Payment.property_id == property_id, Payment.status == "completed")
            .scalar() or 0
        ),
        "pending_payments": Payment.query.filter_by(property_id=property_id, status="pending").count(),
        "overdue_payments": Payment.query.filter_by(property_id=property_id, status="overdue").count(),
    }

    room_data = []
    for r in rooms:
        assignments = RoomTenant.query.filter_by(room_id=r.id, is_active=True).all()
        paid_cnt = sum(1 for a in assignments if a.payment_status == "paid")
        room_data.append({
            "room": r, "assignments": assignments,
            "occupancy": len(assignments),
            "vacant": max(0, r.max_capacity - len(assignments)),
            "paid_count": paid_cnt,
        })

    target_month = request.args.get("month", fmt_month())
    pay_summary = _payment_svc.owner_summary(current_user.id, target_month, property_id=property_id)

    recent_payments = Payment.query.filter_by(property_id=property_id).order_by(Payment.created_at.desc()).limit(10).all()

    return render_template("owner/dashboard.html",
                           stats=stats, recent_payments=recent_payments,
                           props=[prop], room_data=room_data,
                           pay_summary=pay_summary, target_month=target_month,
                           current_property=prop)


@owner_bp.route("/property-selection")
@login_required
@role_required("owner")
def property_selection():
    mark_overdue_payments()

    props = Property.query.filter_by(owner_id=current_user.id, is_deleted=False).all()
    p_ids = [p.id for p in props]

    # Global stats across all properties
    stats = {
        "total_properties": len(props),
        "total_tenants": User.query.filter_by(owner_id=current_user.id, role="tenant").count(),
        "vacant_rooms": Room.query.filter_by(owner_id=current_user.id, is_active=True).count() - RoomTenant.query.filter(RoomTenant.is_active == True, RoomTenant.room_id.in_([r.id for r in Room.query.filter_by(owner_id=current_user.id, is_active=True).all()])).count(),
        "monthly_revenue": float(
            db.session.query(db.func.coalesce(db.func.sum(Payment.amount), 0))
            .filter(Payment.property_id.in_(p_ids), Payment.status == "completed")
            .scalar() or 0
        ) if p_ids else 0.0,
        "pending_payments": Payment.query.filter(
            Payment.property_id.in_(p_ids), Payment.status == "pending"
        ).count() if p_ids else 0,
        "maintenance_issues": 0,  # Placeholder, can add later
    }

    # Property list with summaries
    property_data = []
    for prop in props:
        tenant_count = PropertyTenant.query.filter_by(property_id=prop.id, status="active").count()
        rooms = Room.query.filter_by(property_id=prop.id, is_active=True).all()
        vacant_rooms = len(rooms) - RoomTenant.query.filter(RoomTenant.is_active == True, RoomTenant.room_id.in_([r.id for r in rooms])).count()
        revenue_this_month = float(
            db.session.query(db.func.coalesce(db.func.sum(Payment.amount), 0))
            .filter(Payment.property_id == prop.id, Payment.status == "completed", Payment.rent_month == fmt_month())
            .scalar() or 0
        )
        pending_dues = Payment.query.filter_by(property_id=prop.id, status="pending").count()

        # Status indicator
        if pending_dues > 0:
            status = "attention"
        elif vacant_rooms == 0:
            status = "critical"
        else:
            status = "healthy"

        property_data.append({
            "property": prop,
            "tenant_count": tenant_count,
            "vacant_rooms": vacant_rooms,
            "revenue_this_month": revenue_this_month,
            "pending_dues": pending_dues,
            "status": status,
        })

    from datetime import datetime
    return render_template(
        "owner/property_selection.html",
        stats=stats,
        property_data=property_data,
        now=datetime.now(),
        show_sidebar=False,
    )
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
        from static.utils.validators import require_string, require_amount, optional_string
        from services.tenant_id import slug_property_code
        name    = require_string(request.form.get("name"),    "name", max_len=200)
        address = require_string(request.form.get("address"), "address")
        city    = require_string(request.form.get("city"),    "city", max_len=100)
        rent    = require_amount(request.form.get("monthly_rent"), "monthly_rent")
    except ValidationError as e:
        flash(str(e), "error")
        return redirect(url_for('owner.property_selection'))

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
        db.session.flush()
        prop.short_code = slug_property_code(prop)[:16]
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


@owner_bp.route("/api/property/<int:pid>/rooms")
@login_required
@role_required("owner")
def api_property_rooms(pid):
    """JSON: rooms with live occupancy for add-tenant form."""
    prop = Property.query.filter_by(
        id=pid, owner_id=current_user.id, is_deleted=False
    ).first_or_404()
    rooms = (
        Room.query.filter_by(property_id=pid, is_active=True)
        .order_by(Room.room_number)
        .all()
    )
    return jsonify(
        {
            "ok": True,
            "property_id": prop.id,
            "property_name": prop.name,
            "rooms": [
                {
                    "id": r.id,
                    "room_number": r.room_number,
                    "floor": r.floor,
                    "occupancy": r.get_occupancy(),
                    "max_capacity": r.max_capacity,
                    "vacant_slots": r.get_vacant_slots(),
                    "is_full": r.get_is_full(),
                }
                for r in rooms
            ],
        }
    )


@owner_bp.route("/tenants/add", methods=["POST"])
@login_required
@role_required("owner")
def add_tenant():
    try:
        data   = validate_create_tenant(request.form)
        
        # Add address to tenant data
        data["address"] = optional_string(request.form.get("address"), "address", max_len=500)
        
        tenant = _tenant_svc.create(data, owner_id=current_user.id)

        # Handle file uploads for photo and proof
        from flask import current_app
        from services import (save_uploaded_file, get_photo_filename, 
                             get_proof_filename, check_verification_status)
        
        upload_folder = current_app.config.get("UPLOAD_FOLDER", "static/uploads")
        
        photo_path = None
        proof_path = None
        
        # Save photo if uploaded
        if request.files.get("photo") and request.files["photo"].filename:
            photo_filename = get_photo_filename(tenant.full_name)
            photo_path = save_uploaded_file(
                request.files["photo"], 
                upload_folder, 
                photo_filename
            )
        
        # Save proof if uploaded
        if request.files.get("proof_id") and request.files["proof_id"].filename:
            proof_filename = get_proof_filename(tenant.full_name)
            proof_path = save_uploaded_file(
                request.files["proof_id"], 
                upload_folder, 
                proof_filename
            )
        
        # Update tenant with file paths and check verification
        if photo_path or proof_path or data.get("address"):
            from models import db
            tenant.photo = photo_path
            tenant.proof_id = proof_path
            tenant.is_verified = check_verification_status(
                data.get("address"), photo_path, proof_path
            )
            db.session.commit()

        # Optional property + room assignment
        prop_id = optional_id(request.form.get("property_id"), "property_id")
        room_raw = (request.form.get("room_id") or "").strip()
        room_id = None
        if room_raw:
            try:
                room_id = int(room_raw)
            except ValueError:
                room_id = None
        if room_id and not prop_id:
            flash("Select a property before choosing a room.", "error")
            return redirect(url_for("owner.tenants"))
        if prop_id:
            ls = optional_date(request.form.get("lease_start"), "lease_start")
            le = optional_date(request.form.get("lease_end"),   "lease_end")
            dep = optional_amount(request.form.get("deposit"), "deposit") if request.form.get("deposit") else None
            try:
                _tenant_svc.assign_to_property(
                    tenant.id, prop_id, current_user.id,
                    lease_start=ls, lease_end=le, deposit_amount=dep,
                    room_id=room_id,
                )
            except AppError as e:
                flash(f"Tenant created but property assignment failed: {e}", "error")
                return redirect(url_for("owner.tenants"))

        # Welcome notification
        try:
            from app import socketio
            u = User.query.get(tenant.id)
            extra = f" ID: {u.tenant_public_id}" if u and u.tenant_public_id else ""
            push_notification(
                socketio, tenant.id, "Welcome!",
                f"Hello {tenant.full_name}, your account is ready. Login: {tenant.phone}{extra}.",
                "chat" if (u and u.tenant_public_id) else "general",
            )
        except Exception:
            pass

        db_tenant = User.query.get(tenant.id)
        tid = db_tenant.tenant_public_id if db_tenant else None
        msg = f"Tenant '{tenant.full_name}' created. Login: {tenant.phone}"
        if tid:
            msg += f" · ID: {tid}"
        flash(msg, "success")

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


@owner_bp.route("/tenants/<int:tid>/profile")
@login_required
@role_required("owner")
def tenant_profile(tid):
    """Display tenant profile with verification status."""
    tenant = User.query.filter_by(id=tid, owner_id=current_user.id, role="tenant").first_or_404()
    
    # Recalculate verification status
    from services import check_verification_status
    tenant.is_verified = check_verification_status(
        tenant.address, tenant.photo, tenant.proof_id
    )
    db.session.commit()
    
    # Get tenant's property assignments
    from models import PropertyTenant
    tenancies = PropertyTenant.query.filter_by(tenant_id=tid).all()
    
    # Get payment history
    history = _payment_svc.history(tenant.id, months=12)
    
    return render_template("owner/tenant_profile.html", 
                           tenant=tenant, 
                           tenancies=tenancies,
                           history=history)


@owner_bp.route("/tenants/<int:tid>/edit-profile", methods=["GET", "POST"])
@login_required
@role_required("owner")
def edit_tenant_profile(tid):
    """Edit tenant profile including address, photo, and proof ID."""
    tenant = User.query.filter_by(id=tid, owner_id=current_user.id, role="tenant").first_or_404()
    
    if request.method == "POST":
        try:
            from flask import current_app
            from services import save_uploaded_file, get_photo_filename, get_proof_filename, check_verification_status
            
            # Update basic info
            if request.form.get("full_name"):
                tenant.full_name = request.form.get("full_name")
            
            if request.form.get("address"):
                tenant.address = request.form.get("address")
            
            # Handle photo upload
            if request.files.get("photo") and request.files["photo"].filename:
                upload_folder = current_app.config.get("UPLOAD_FOLDER", "static/uploads")
                photo_filename = get_photo_filename(tenant.full_name)
                photo_path = save_uploaded_file(request.files["photo"], upload_folder, photo_filename)
                tenant.photo = photo_path
            
            # Handle proof ID upload
            if request.files.get("proof_id") and request.files["proof_id"].filename:
                upload_folder = current_app.config.get("UPLOAD_FOLDER", "static/uploads")
                proof_filename = get_proof_filename(tenant.full_name)
                proof_path = save_uploaded_file(request.files["proof_id"], upload_folder, proof_filename)
                tenant.proof_id = proof_path
            
            # Recalculate verification status
            tenant.is_verified = check_verification_status(
                tenant.address, tenant.photo, tenant.proof_id
            )
            
            db.session.commit()
            flash("✅ Profile updated successfully!", "success")
            return redirect(url_for("owner.tenant_profile", tid=tid))
        except Exception as e:
            db.session.rollback()
            flash(f"Error updating profile: {str(e)}", "error")
            return redirect(url_for("owner.edit_tenant_profile", tid=tid))
    
    return render_template("owner/edit_tenant_profile.html", tenant=tenant)


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
    from static.utils.validators import require_id, require_string
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
