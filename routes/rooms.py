"""Rooms routes — delegate all logic to RoomService."""
from flask import Blueprint, render_template, request, redirect, url_for, flash, jsonify
from flask_login import login_required, current_user
from models import User, Property, Room, RoomTenant
from routes import role_required
from services.room import RoomService
from services.notification import NotificationService
from utils.validators import validate_add_room, require_payment_status
from utils.errors import AppError, api_ok, api_error

rooms_bp  = Blueprint("rooms", __name__, url_prefix="/rooms")
_room_svc = RoomService()
_notif_svc = NotificationService()


def _owner_rooms():
    if current_user.role == "admin":
        return Room.query.filter_by(is_active=True)
    return Room.query.filter_by(owner_id=current_user.id, is_active=True)


@rooms_bp.route("/")
@login_required
@role_required("owner", "admin")
def index():
    rooms     = _owner_rooms().order_by(Room.room_number).all()
    room_data = []
    for r in rooms:
        assignments = r.room_tenants.filter_by(is_active=True).all()
        occ         = len(assignments)
        paid_cnt    = sum(1 for a in assignments if a.payment_status == "paid")
        room_data.append({
            "room": r, "assignments": assignments,
            "occupancy": occ, "vacant": max(0, r.max_capacity - occ),
            "paid_count": paid_cnt,
        })

    properties = (Property.query.filter_by(is_deleted=False).all()
                  if current_user.role == "admin" else
                  Property.query.filter_by(owner_id=current_user.id,
                                           is_deleted=False).all())
    tenants    = (User.query.filter_by(role="tenant", is_active=True).all()
                  if current_user.role == "admin" else
                  User.query.filter_by(role="tenant", owner_id=current_user.id,
                                       is_active=True).all())

    return render_template("rooms/index.html",
                           room_data=room_data,
                           properties=properties,
                           tenants=tenants)


@rooms_bp.route("/add", methods=["POST"])
@login_required
@role_required("owner", "admin")
def add_room():
    try:
        data = validate_add_room(request.form)
        _room_svc.create(data, owner_id=current_user.id)
        flash(f"Room {data['room_number']} created.", "success")
    except AppError as e:
        flash(str(e), "error")
    return redirect(url_for("rooms.index"))


@rooms_bp.route("/<int:rid>/edit", methods=["POST"])
@login_required
@role_required("owner", "admin")
def edit_room(rid):
    try:
        _room_svc.edit(rid, owner_id=current_user.id, data=request.form)
        flash("Room updated.", "success")
    except AppError as e:
        flash(str(e), "error")
    return redirect(url_for("rooms.index"))


@rooms_bp.route("/<int:rid>/delete", methods=["POST"])
@login_required
@role_required("owner", "admin")
def delete_room(rid):
    try:
        _room_svc.deactivate(rid, owner_id=current_user.id)
        flash("Room removed.", "success")
    except AppError as e:
        flash(str(e), "error")
    return redirect(url_for("rooms.index"))


@rooms_bp.route("/<int:rid>/assign", methods=["POST"])
@login_required
@role_required("owner", "admin")
def assign_tenant(rid):
    from utils.validators import optional_id
    tenant_id = optional_id(request.form.get("tenant_id"), "tenant_id")
    ok, msg, _ = _room_svc.assign_tenant(rid, tenant_id, current_user.id)
    flash(msg, "success" if ok else "error")
    return redirect(url_for("rooms.index"))


@rooms_bp.route("/assignment/<int:aid>/remove", methods=["POST"])
@login_required
@role_required("owner", "admin")
def remove_tenant(aid):
    try:
        _room_svc.remove_tenant(aid, owner_id=current_user.id)
        flash("Tenant removed from room.", "success")
    except AppError as e:
        flash(str(e), "error")
    return redirect(url_for("rooms.index"))


@rooms_bp.route("/assignment/<int:aid>/payment", methods=["POST"])
@login_required
@role_required("owner", "admin")
def update_payment(aid):
    try:
        new_status = request.form.get("payment_status", "not_paid")
        rt = _room_svc.update_payment_status(aid, new_status, owner_id=current_user.id)

        # Real-time push to tenant
        if rt.tenant_id:
            try:
                from app import socketio
                label = "Paid ✓" if rt.payment_status == "paid" else "Not Paid"
                _notif_svc.push(
                    socketio, rt.tenant_id,
                    title=f"Room Payment: {label}",
                    body=f"Your room payment status updated to {label}.",
                    notif_type="payment_received" if rt.payment_status == "paid" else "payment_due",
                )
            except Exception:
                pass

        flash("Payment status updated.", "success")
    except AppError as e:
        flash(str(e), "error")
    return redirect(url_for("rooms.index"))


@rooms_bp.route("/api/summary")
@login_required
@role_required("owner", "admin")
def api_summary():
    rooms = _owner_rooms().all()
    return jsonify({"ok": True, "data": [r.to_dict() for r in rooms]})
