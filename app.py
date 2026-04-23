"""
PropFlow — Production Flask + SocketIO
Run: python app.py
"""
import os, uuid, logging
from datetime import datetime, timedelta, timezone
from flask import Flask, jsonify, render_template, request
from flask_login import LoginManager
from flask_socketio import SocketIO, join_room,test_client, emit, leave_room
from werkzeug.utils import secure_filename

from config import Config
from models import db, User
from utils.logger import configure_logging, get_logger
from utils.errors import AppError

log = get_logger(__name__)
socketio = SocketIO()


def create_app(config_class=Config):
    configure_logging("INFO")

    app = Flask(__name__)
    app.config.from_object(config_class)
    os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)

    db.init_app(app)
    socketio.init_app(
        app,
        cors_allowed_origins="*",
        async_mode="threading",
        logger=False, engineio_logger=False,
        ping_timeout=60, ping_interval=25,
    )

    # ── Flask-Login ────────────────────────────────────────────────────────────
    lm = LoginManager()
    lm.init_app(app)
    lm.login_view = "auth.login"
    lm.login_message = "Please log in."
    lm.login_message_category = "info"

    @lm.user_loader
    def load_user(uid):
        try:
            return User.query.get(int(uid))
        except Exception:
            return None   # never crash the session loader

    # ── Blueprints ─────────────────────────────────────────────────────────────
    from routes.auth   import auth_bp
    from routes.admin  import admin_bp
    from routes.owner  import owner_bp
    from routes.tenant import tenant_bp
    from routes.chat   import chat_bp
    from routes.rooms  import rooms_bp

    for bp in [auth_bp, admin_bp, owner_bp, tenant_bp, chat_bp, rooms_bp]:
        app.register_blueprint(bp)

    # ── Global error handlers ──────────────────────────────────────────────────
    @app.errorhandler(AppError)
    def handle_app_error(err: AppError):
        err.log()
        return jsonify(err.to_dict()), err.http_status

    @app.errorhandler(400)
    def bad_request(e):
        return jsonify({"ok": False, "code": "BAD_REQUEST",
                        "error": "Bad request"}), 400

    @app.errorhandler(403)
    def forbidden(e):
        if request.path.startswith("/api/"):
            return jsonify({"ok": False, "code": "FORBIDDEN",
                            "error": "Access denied"}), 403
        return render_template("errors/403.html"), 403

    @app.errorhandler(404)
    def not_found(e):
        if request.path.startswith("/api/"):
            return jsonify({"ok": False, "code": "NOT_FOUND",
                            "error": "Resource not found"}), 404
        return render_template("errors/404.html"), 404

    @app.errorhandler(413)
    def too_large(e):
        if request.path.startswith("/api/"):
            return jsonify({"ok": False, "code": "FILE_TOO_LARGE",
                            "error": "File too large (max 10 MB)"}), 413
        return render_template("errors/413.html"), 413

    @app.errorhandler(500)
    def internal_error(e):
        db.session.rollback()   # always rollback on 500
        log.exception("Unhandled 500 error")
        if request.path.startswith("/api/"):
            return jsonify({"ok": False, "code": "INTERNAL_ERROR",
                            "error": "An internal error occurred."}), 500
        return render_template("errors/500.html"), 500

    @app.errorhandler(Exception)
    def handle_unhandled(exc):
        db.session.rollback()
        log.exception(f"Unhandled exception: {type(exc).__name__}: {exc}")
        return jsonify({"ok": False, "code": "INTERNAL_ERROR",
                        "error": "An unexpected error occurred."}), 500

    log.info("PropFlow app created", extra={"db": app.config["SQLALCHEMY_DATABASE_URI"][:30]})
    return app


# ── SocketIO event: join user's personal room ──────────────────────────────────
@socketio.on("join_user_room")
def handle_join_user_room():
    from flask_login import current_user
    if current_user.is_authenticated:
        join_room(f"user_{current_user.id}")


# ── Seed data ──────────────────────────────────────────────────────────────────
def seed(app):
    with app.app_context():
        db.create_all()
        if User.query.count() > 0:
            log.info("DB already seeded")
            return

        from models import (Property, PropertyTenant, Payment,
                             Notification, Room, RoomTenant)
        from services.helpers import fmt_month

        now = datetime.now(timezone.utc).replace(tzinfo=None)

        admin = User(phone=app.config["ADMIN_PHONE"],
                     full_name="System Admin", role="admin")
        admin.set_password(app.config["ADMIN_PASSWORD"])
        db.session.add(admin)

        o1 = User(phone="owner1", full_name="Rahul Sharma",  role="owner")
        o1.set_password("owner123")
        o2 = User(phone="owner2", full_name="Priya Mehta",   role="owner")
        o2.set_password("owner123")
        db.session.add_all([o1, o2])
        db.session.flush()

        tenants = []
        for ph, name in [("tenant1","Alice Johnson"),
                         ("tenant2","Bob Smith"),
                         ("tenant3","Carol Davis")]:
            t = User(phone=ph, full_name=name, role="tenant", owner_id=o1.id)
            t.set_password("tenant123")
            db.session.add(t)
            tenants.append(t)

        t4 = User(phone="tenant4", full_name="David Wilson", role="tenant", owner_id=o2.id)
        t4.set_password("tenant123")
        db.session.add(t4)
        db.session.flush()

        props = [
            Property(name="Sunrise PG Block-A", address="12 MG Road",
                     city="Bengaluru", state="KA", unit_number="A",
                     property_type="pg", bedrooms=4, bathrooms=2,
                     area_sqft=1200, monthly_rent=6000,
                     status="occupied", owner_id=o1.id),
            Property(name="Sunrise PG Block-B", address="12 MG Road",
                     city="Bengaluru", state="KA", unit_number="B",
                     property_type="pg", bedrooms=4, bathrooms=2,
                     area_sqft=1100, monthly_rent=5500,
                     status="occupied", owner_id=o1.id),
            Property(name="Green View Hostel", address="45 Anna Salai",
                     city="Chennai", state="TN", property_type="hostel",
                     bedrooms=6, bathrooms=3, area_sqft=2000,
                     monthly_rent=4500, status="available", owner_id=o1.id),
            Property(name="City PG Rooms", address="78 FC Road",
                     city="Pune", state="MH", property_type="pg",
                     bedrooms=3, bathrooms=2, area_sqft=900,
                     monthly_rent=7000, status="occupied", owner_id=o2.id),
        ]
        db.session.add_all(props)
        db.session.flush()

        rooms = [
            Room(room_number="101", max_capacity=4, description="Ground Floor A",
                 floor="Ground", property_id=props[0].id, owner_id=o1.id),
            Room(room_number="102", max_capacity=3, description="Ground Floor B",
                 floor="Ground", property_id=props[0].id, owner_id=o1.id),
            Room(room_number="201", max_capacity=4, description="First Floor",
                 floor="1st",    property_id=props[1].id, owner_id=o1.id),
            Room(room_number="301", max_capacity=4, description="Main Room",
                 floor="Ground", property_id=props[3].id, owner_id=o2.id),
        ]
        db.session.add_all(rooms)
        db.session.flush()

        db.session.add_all([
            PropertyTenant(property_id=props[0].id, tenant_id=tenants[0].id,
                room_id=rooms[0].id, room_number="101",
                lease_start=now - timedelta(days=180),
                lease_end=now + timedelta(days=185),
                deposit_amount=12000, status="active"),
            PropertyTenant(property_id=props[0].id, tenant_id=tenants[1].id,
                room_id=rooms[0].id, room_number="101",
                lease_start=now - timedelta(days=90),
                lease_end=now + timedelta(days=275),
                deposit_amount=11000, status="active"),
            PropertyTenant(property_id=props[3].id, tenant_id=t4.id,
                room_id=rooms[3].id, room_number="301",
                lease_start=now - timedelta(days=60),
                lease_end=now + timedelta(days=305),
                deposit_amount=14000, status="active"),
        ])
        db.session.add_all([
            RoomTenant(room_id=rooms[0].id, tenant_id=tenants[0].id, payment_status="paid"),
            RoomTenant(room_id=rooms[0].id, tenant_id=tenants[1].id, payment_status="not_paid"),
            RoomTenant(room_id=rooms[3].id, tenant_id=t4.id, payment_status="paid"),
        ])

        cur_month  = fmt_month()
        prev_month = fmt_month(now - timedelta(days=32))

        def mkpay(tid, pid, amt, ptype, status, month, paid=False):
            yr, mo = int(month[:4]), int(month[5:])
            return Payment(
                tenant_id=tid, property_id=pid, amount=amt,
                payment_type=ptype, status=status, rent_month=month,
                due_date=datetime(yr, mo, 1),
                paid_at=now - timedelta(days=3) if paid else None,
                transaction_id=f"TXN-{uuid.uuid4().hex[:10].upper()}" if paid else None,
                payment_method="online" if paid else None,
                description=f"Rent — {month}",
            )

        db.session.add_all([
            mkpay(tenants[0].id, props[0].id, 6000, "rent", "completed", prev_month, True),
            mkpay(tenants[0].id, props[0].id, 6000, "rent", "pending",   cur_month),
            mkpay(tenants[1].id, props[0].id, 6000, "rent", "completed", prev_month, True),
            mkpay(tenants[1].id, props[0].id, 6000, "rent", "overdue",   cur_month),
            mkpay(t4.id,         props[3].id, 7000, "rent", "completed", prev_month, True),
            mkpay(t4.id,         props[3].id, 7000, "rent", "pending",   cur_month),
        ])
        db.session.add_all([
            Notification(user_id=tenants[0].id, title="Rent Due",
                body=f"Your rent of ₹6,000 for {cur_month} is due.",
                notif_type="payment_due"),
            Notification(user_id=tenants[1].id, title="⚠️ Payment Overdue",
                body=f"Your rent of ₹6,000 for {cur_month} is overdue.",
                notif_type="payment_overdue"),
            Notification(user_id=t4.id, title="Welcome!",
                body="Your room at City PG is ready. Login: tenant4 / tenant123",
                notif_type="general"),
        ])
        db.session.commit()

        log.info("Seed complete", extra={
            "users": User.query.count(),
            "properties": Property.query.count(),
            "rooms": Room.query.count(),
            "payments": Payment.query.count(),
        })
        print("\n" + "="*50)
        print("  DEMO ACCOUNTS")
        print("  admin   / admin123")
        print("  owner1  / owner123")
        print("  tenant1 / tenant123")
        print("="*50 + "\n")


if __name__ == "__main__":
    app  = create_app()
    seed(app)
    port  = int(os.environ.get("PORT",  5000))
    debug = os.environ.get("DEBUG", "true").lower() == "true"
    log.info(f"Starting on port {port}", extra={"debug": debug})
    socketio.run(app, host="0.0.0.0", port=port,
                 debug=debug, allow_unsafe_werkzeug=True)
