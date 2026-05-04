import uuid
from datetime import datetime
from flask import Blueprint, render_template, request, redirect, url_for, flash, jsonify
from flask_login import login_required, current_user
from models import db, User, Property, PropertyTenant, Payment, Notification, Room, RoomTenant, now_utc
from routes import role_required
from services import (generate_monthly_rent, mark_overdue_payments,
                      owner_payment_summary, push_notification, fmt_month)

admin_bp = Blueprint("admin", __name__, url_prefix="/admin")


@admin_bp.route("/dashboard")
@login_required
@role_required("admin")
def dashboard():
    mark_overdue_payments()

    stats = {
        "total_users":      User.query.filter_by(is_active=True).count(),
        "total_owners":     User.query.filter_by(role="owner",  is_active=True).count(),
        "total_tenants":    User.query.filter_by(role="tenant", is_active=True).count(),
        "total_properties": Property.query.filter_by(is_deleted=False).count(),
        "total_rooms":      Room.query.filter_by(is_active=True).count(),
        "total_payments":   Payment.query.count(),
        "revenue": float(db.session.query(
            db.func.coalesce(db.func.sum(Payment.amount), 0)
        ).filter_by(status="completed").scalar() or 0),
        "pending_payments": Payment.query.filter_by(status="pending").count(),
        "overdue_payments": Payment.query.filter_by(status="overdue").count(),
        "occupied_rooms":   sum(1 for r in Room.query.filter_by(is_active=True).all()
                                if hasattr(r, "get_occupancy") and (r.get_occupancy() or 0) > 0),
                                # if r.get_occupancy() > 0),
    }
    recent_payments = Payment.query.order_by(Payment.created_at.desc()).limit(10).all()
    recent_users    = User.query.order_by(User.created_at.desc()).limit(8).all()
    return render_template("admin/dashboard.html",
                           stats=stats,
                           recent_payments=recent_payments,
                           recent_users=recent_users)


# ── Generate rent (all properties) ───────────────────────────────────────────
@admin_bp.route("/generate-rent", methods=["POST"])
@login_required
@role_required("admin")
def generate_rent():
    month = request.form.get("month", fmt_month())
    created, skipped, errors = generate_monthly_rent(force_month=month)
    if errors:
        flash(f"Errors: {'; '.join(errors[:3])}", "error")
    flash(f"Rent for {month}: {created} created, {skipped} skipped.", "success")
    return redirect(url_for("admin.dashboard"))


# ── Users CRUD (Admin creates OWNERS only) ─────────────────────────────────────
@admin_bp.route("/users")
@login_required
@role_required("admin")
def users():
    role_filter = request.args.get("role", "")
    q = User.query
    if role_filter:
        q = q.filter_by(role=role_filter)
    all_users = q.order_by(User.created_at.desc()).all()
    return render_template("admin/users.html", users=all_users, role_filter=role_filter)


@admin_bp.route("/users/add", methods=["POST"])
@login_required
@role_required("admin")
def add_user():
    phone     = request.form.get("phone", "").strip()
    full_name = request.form.get("full_name", "").strip()
    password  = request.form.get("password", "")

    if not phone or not full_name or not password:
        flash("All fields are required.", "error")
        return redirect(url_for("admin.users"))
    if User.query.filter_by(phone=phone).first():
        flash("Phone number already exists.", "error")
        return redirect(url_for("admin.users"))

    user = User(phone=phone, full_name=full_name, role="owner")
    user.set_password(password)
    db.session.add(user)
    db.session.commit()
    flash(f"Owner '{full_name}' created. Login: {phone}", "success")
    return redirect(url_for("admin.users"))


@admin_bp.route("/users/<int:uid>/edit", methods=["POST"])
@login_required
@role_required("admin")
def edit_user(uid):
    user = User.query.get_or_404(uid)
    user.full_name = request.form.get("full_name", user.full_name).strip()
    user.is_active = request.form.get("is_active") == "1"
    new_pw = request.form.get("password", "")
    if new_pw:
        user.set_password(new_pw)
    db.session.commit()
    flash("User updated.", "success")
    return redirect(url_for("admin.users"))


@admin_bp.route("/users/<int:uid>/delete", methods=["POST"])
@login_required
@role_required("admin")
def delete_user(uid):
    user = User.query.get_or_404(uid)
    if user.id == 1:
        flash("Cannot delete the primary admin.", "error")
        return redirect(url_for("admin.users"))
    if user.role == "owner":
        flash("Cannot delete an owner. Please reassign or delete their properties first.", "error")
        return redirect(url_for("admin.users"))
    try:
        db.session.delete(user)
        db.session.commit()
        flash("User deleted.", "success")
    except Exception as e:
        db.session.rollback()
        flash(f"Error deleting user: {str(e)}", "error")    
        return redirect(url_for("admin.users"))
    db.session.delete(user)
    db.session.commit()
    flash("User deleted.", "success")
    return redirect(url_for("admin.users"))


# ── Payment history per tenant ────────────────────────────────────────────────
@admin_bp.route("/tenants")
@login_required
@role_required("admin")
def tenants():
    """List all tenants for admin."""
    all_tenants = User.query.filter_by(role="tenant").order_by(User.created_at.desc()).all()
    return render_template("admin/tenants.html", tenants=all_tenants)


@admin_bp.route("/tenants/<int:tid>/history")
@login_required
@role_required("admin")
def tenant_history(tid):
    tenant = User.query.filter_by(id=tid, role="tenant").first_or_404()
    from services import tenant_payment_history
    history = tenant_payment_history(tenant.id, months=24)
    return render_template("admin/tenant_history.html", tenant=tenant, history=history)


# ── Tenant Profile (Admin view) ───────────────────────────────────────────────
@admin_bp.route("/tenants/<int:tid>/profile")
@login_required
@role_required("admin")
def tenant_profile(tid):
    """Display tenant profile with verification status for admin."""
    tenant = User.query.filter_by(id=tid, role="tenant").first_or_404()
    
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
    from services import tenant_payment_history
    history = tenant_payment_history(tenant.id, months=12)
    
    return render_template("admin/tenant_profile.html", 
                           tenant=tenant, 
                           tenancies=tenancies,
                           history=history)


@admin_bp.route("/tenants/<int:tid>/edit-profile", methods=["GET", "POST"])
@login_required
@role_required("admin")
def edit_tenant_profile(tid):
    """Edit tenant profile including address, photo, and proof ID for admin."""
    tenant = User.query.filter_by(id=tid, role="tenant").first_or_404()
    
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
            return redirect(url_for("admin.tenant_profile", tid=tid))
        except Exception as e:
            db.session.rollback()
            flash(f"Error updating profile: {str(e)}", "error")
            return redirect(url_for("admin.edit_tenant_profile", tid=tid))
    
    return render_template("admin/edit_tenant_profile.html", tenant=tenant)


# ── Payments ──────────────────────────────────────────────────────────────────
@admin_bp.route("/payments")
@login_required
@role_required("admin")
def payments():
    sf = request.args.get("status", "")
    mf = request.args.get("month", "")
    q  = Payment.query
    if sf: q = q.filter_by(status=sf)
    if mf: q = q.filter_by(rent_month=mf)
    all_payments = q.order_by(Payment.rent_month.desc(), Payment.created_at.desc()).all()

    months = [r[0] for r in
              db.session.query(Payment.rent_month)
              .filter(Payment.rent_month.isnot(None))
              .distinct().order_by(Payment.rent_month.desc()).all()]

    summary = {s: {"count": 0, "total": 0.0}
               for s in ["pending","completed","overdue","failed"]}
    for p in all_payments:
        if p.status in summary:
            summary[p.status]["count"] += 1
            summary[p.status]["total"] += float(p.amount)

    tenants    = User.query.filter_by(role="tenant", is_active=True).all()
    properties = Property.query.filter_by(is_deleted=False).all()
    return render_template("admin/payments.html",
                           payments=all_payments, summary=summary,
                           tenants=tenants, properties=properties,
                           status_filter=sf, month_filter=mf,
                           available_months=months)


@admin_bp.route("/payments/add", methods=["POST"])
@login_required
@role_required("admin")
def add_payment():
    due_raw  = request.form.get("due_date", "")
    due_date = datetime.strptime(due_raw, "%Y-%m-%d") if due_raw else None
    month    = request.form.get("rent_month", fmt_month())
    pay = Payment(
        tenant_id=int(request.form["tenant_id"]),
        property_id=int(request.form["property_id"]),
        amount=float(request.form["amount"]),
        payment_type=request.form.get("payment_type", "rent"),
        status="pending", rent_month=month, due_date=due_date,
        description=request.form.get("description", ""),
    )
    db.session.add(pay)
    db.session.commit()
    flash("Payment created.", "success")
    return redirect(url_for("admin.payments"))


@admin_bp.route("/payments/<int:pid>/update-status", methods=["POST"])
@login_required
@role_required("admin")
def update_payment_status(pid):
    pay = Payment.query.get_or_404(pid)
    pay.status = request.form.get("status", pay.status)
    if pay.status == "completed" and not pay.paid_at:
        pay.paid_at        = now_utc()
        pay.transaction_id = pay.transaction_id or f"TXN-{uuid.uuid4().hex[:10].upper()}"
    db.session.commit()
    flash("Payment updated.", "success")
    return redirect(url_for("admin.payments"))


@admin_bp.route("/payments/<int:pid>/delete", methods=["POST"])
@login_required
@role_required("admin")
def delete_payment(pid):
    db.session.delete(Payment.query.get_or_404(pid))
    db.session.commit()
    flash("Payment deleted.", "success")
    return redirect(url_for("admin.payments"))


# ── Properties ────────────────────────────────────────────────────────────────
@admin_bp.route("/properties")
@login_required
@role_required("admin")
def properties():
    props  = Property.query.filter_by(is_deleted=False).order_by(Property.created_at.desc()).all()
    owners = User.query.filter_by(role="owner", is_active=True).all()
    return render_template("admin/properties.html", properties=props, owners=owners)


@admin_bp.route("/properties/<int:pid>/delete", methods=["POST"])
@login_required
@role_required("admin")
def delete_property(pid):
    prop = Property.query.get_or_404(pid)
    prop.is_deleted = True
    db.session.commit()
    flash("Property removed.", "success")
    return redirect(url_for("admin.properties"))


# ── JSON APIs ─────────────────────────────────────────────────────────────────
@admin_bp.route("/api/notifications/unread")
@login_required
@role_required("admin")
def unread_notifications():
    count = Notification.query.filter_by(user_id=current_user.id, is_read=False).count()
    return jsonify({"count": count})


@admin_bp.route("/api/overview")
@login_required
@role_required("admin")
def api_overview():
    return jsonify({
        "total_rooms":   Room.query.filter_by(is_active=True).count(),
        "occupied_rooms": sum(1 for r in Room.query.filter_by(is_active=True).all()
                              if r.get_occupancy() > 0),
        "pending":        Payment.query.filter_by(status="pending").count(),
        "overdue":        Payment.query.filter_by(status="overdue").count(),
    })
