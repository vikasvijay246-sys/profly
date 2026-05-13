"""
Chat routes — thin layer calling MessageService.
HTTP send + SocketIO events.
"""
from datetime import datetime

from flask import (
    Blueprint,
    render_template,
    request,
    jsonify,
    current_app,
    make_response,
)
from flask_login import login_required, current_user
from flask_socketio import emit, join_room, leave_room

from models import db, User, Message, Room, RoomTenant
from services.message import (
    MessageService,
    _room_key,
    rgrp_room_key,
    PAGE_SIZE,
)
from static.utils.validators import require_id
from utils.errors import AppError, PermissionError_, api_error, api_ok, handle_unexpected

chat_bp = Blueprint("chat", __name__, url_prefix="/chat")
_msg_svc = MessageService()


def _rooms_for_chat(user: User) -> list:
    """Rooms visible for property / roommate chat."""
    if user.role == "owner":
        return (
            Room.query.filter_by(owner_id=user.id, is_active=True)
            .order_by(Room.property_id, Room.room_number)
            .all()
        )
    if user.role == "tenant":
        if not user.is_verified:
            return []
        rids = (
            db.session.query(RoomTenant.room_id)
            .filter_by(tenant_id=user.id, is_active=True)
            .distinct()
            .all()
        )
        ids = [r[0] for r in rids]
        if not ids:
            return []
        return Room.query.filter(Room.id.in_(ids), Room.is_active == True).all()
    return []


@chat_bp.route("/")
@login_required
def index():
    # Build DM contact list
    if current_user.role == "admin":
        users = User.query.filter(
            User.id != current_user.id, User.is_active == True
        ).all()
    elif current_user.role == "owner":
        admins = User.query.filter_by(role="admin", is_active=True).all()
        tenants = User.query.filter_by(
            owner_id=current_user.id, role="tenant", is_active=True
        ).all()
        users = admins + tenants
    else:
        admins = User.query.filter_by(role="admin", is_active=True).all()
        owners = []
        if current_user.owner_id:
            o = User.query.get(current_user.owner_id)
            if o and o.is_active:
                owners = [o]
        users = admins + owners

    conversations = []
    for u in users:
        room = _room_key(current_user.id, u.id)
        last = (
            Message.query.filter_by(room_id=room, is_deleted=False)
            .order_by(Message.created_at.desc())
            .first()
        )
        unread = (
            Message.query.filter_by(
                room_id=room,
                receiver_id=current_user.id,
                is_read=False,
                is_deleted=False,
            ).count()
        )
        last_created_at = (
            last.created_at.isoformat() + "Z" if last and last.created_at else None
        )
        conversations.append(
            {
                "user": u,
                "last": last,
                "unread": unread,
                "last_created_at": last_created_at,
            }
        )

    conversations.sort(
        key=lambda c: c["last"].created_at if c["last"] else datetime.min,
        reverse=True,
    )

    room_list = _rooms_for_chat(current_user)
    room_conversations = []
    for rm in room_list:
        key = rgrp_room_key(rm.id)
        last = (
            Message.query.filter_by(room_id=key, is_deleted=False)
            .order_by(Message.created_at.desc())
            .first()
        )
        last_created_at = (
            last.created_at.isoformat() + "Z" if last and last.created_at else None
        )
        prop_name = rm.prop.name if rm.prop else "Property"
        label = f"{prop_name} · Room {rm.room_number}"
        room_conversations.append(
            {
                "room": rm,
                "label": label,
                "last": last,
                "last_created_at": last_created_at,
            }
        )

    room_conversations.sort(
        key=lambda c: c["last"].created_at if c["last"] else datetime.min,
        reverse=True,
    )

    selected_uid = request.args.get("with", type=int)
    selected_rid = request.args.get("room", type=int)

    selected_user = None
    messages = []
    selected_room = None
    messages_room = []

    if selected_rid:
        try:
            selected_room = _msg_svc.assert_room_access(
                current_user.id, selected_rid
            )
            messages_room = _msg_svc.load_room_messages(selected_rid)
        except AppError:
            selected_room = None
            messages_room = []
    elif selected_uid:
        selected_user = User.query.filter_by(
            id=selected_uid, is_active=True
        ).first()
        if selected_user:
            messages = _msg_svc.load_conversation(current_user.id, selected_uid)
            _msg_svc.mark_read(
                _room_key(current_user.id, selected_uid), current_user.id
            )

    chat_blocked = current_user.role == "tenant" and not current_user.is_verified

    response = make_response(
        render_template(
            "chat/index.html",
            conversations=conversations,
            room_conversations=room_conversations,
            selected_user=selected_user,
            selected_room=selected_room,
            messages=messages,
            messages_room=messages_room,
            page_size=PAGE_SIZE,
            chat_blocked=chat_blocked,
        )
    )
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response


@chat_bp.route("/send", methods=["POST"])
@login_required
def send():
    try:
        if current_user.role == "tenant" and not current_user.is_verified:
            return api_error(
                PermissionError_(
                    "Complete verification (address, photo, ID proof) to use chat.",
                )
            )

        receiver_id = require_id(request.form.get("receiver_id"), "receiver_id")
        content = request.form.get("content", "").strip() or None
        file_obj = request.files.get("file")

        if file_obj and file_obj.filename:
            msg = _msg_svc.send_file(
                sender_id=current_user.id,
                receiver_id=receiver_id,
                file_obj=file_obj,
                upload_dir=current_app.config["UPLOAD_FOLDER"],
                allowed_exts=current_app.config["ALLOWED_EXTENSIONS"],
                content=content,
            )
        elif content:
            msg = _msg_svc.send_text(current_user.id, receiver_id, content)
        else:
            return api_error(AppError("Message content or file is required"))

        msg_dict = msg.to_dict()

        try:
            from app import socketio

            room = _room_key(current_user.id, receiver_id)
            socketio.emit("new_message", msg_dict, room=f"chat_{room}")
        except Exception as e:
            print("SocketIO emit failed:", e)

        return api_ok(msg_dict, status=201)

    except AppError as e:
        return api_error(e)
    except Exception as exc:
        return handle_unexpected(exc, "send message")


@chat_bp.route("/room/<int:rid>/send", methods=["POST"])
@login_required
def send_room(rid):
    try:
        _msg_svc.assert_room_access(current_user.id, rid)
        content = request.form.get("content", "").strip() or None
        file_obj = request.files.get("file")

        if file_obj and file_obj.filename:
            msg = _msg_svc.send_room_file(
                sender_id=current_user.id,
                room_id=rid,
                file_obj=file_obj,
                upload_dir=current_app.config["UPLOAD_FOLDER"],
                allowed_exts=current_app.config["ALLOWED_EXTENSIONS"],
                content=content,
            )
        elif content:
            msg = _msg_svc.send_room_text(current_user.id, rid, content)
        else:
            return api_error(AppError("Message content or file is required"))

        msg_dict = msg.to_dict()
        try:
            from app import socketio

            key = rgrp_room_key(rid)
            socketio.emit("new_message", msg_dict, room=f"chat_{key}")
        except Exception as e:
            print("SocketIO room emit failed:", e)

        return api_ok(msg_dict, status=201)
    except AppError as e:
        return api_error(e)
    except Exception as exc:
        return handle_unexpected(exc, "send room message")


@chat_bp.route("/poll/<int:other_uid>")
@login_required
def poll(other_uid):
    since_ts = request.args.get("since", 0, type=float)
    since = datetime.fromtimestamp(since_ts) if since_ts else datetime.min
    room = _room_key(current_user.id, other_uid)
    msgs = (
        Message.query.filter_by(room_id=room, is_deleted=False)
        .filter(Message.created_at > since)
        .order_by(Message.created_at.asc())
        .limit(100)
        .all()
    )
    _msg_svc.mark_read(room, current_user.id)
    return jsonify([m.to_dict() for m in msgs])


@chat_bp.route("/room/<int:rid>/poll")
@login_required
def poll_room(rid):
    try:
        _msg_svc.assert_room_access(current_user.id, rid)
        since_ts = request.args.get("since", 0, type=float)
        since = datetime.fromtimestamp(since_ts) if since_ts else datetime.min
        key = rgrp_room_key(rid)
        msgs = (
            Message.query.filter_by(room_id=key, is_deleted=False)
            .filter(Message.created_at > since)
            .order_by(Message.created_at.asc())
            .limit(100)
            .all()
        )
        return jsonify([m.to_dict() for m in msgs])
    except AppError as e:
        return api_error(e)


@chat_bp.route("/messages/<int:other_uid>/older")
@login_required
def load_older(other_uid):
    before_id = request.args.get("before", type=int)
    if not before_id:
        return jsonify([])
    try:
        msgs = _msg_svc.load_conversation(
            current_user.id, other_uid, before_id=before_id
        )
        return jsonify([m.to_dict() for m in msgs])
    except AppError as e:
        return api_error(e)


@chat_bp.route("/room/<int:rid>/older")
@login_required
def load_older_room(rid):
    before_id = request.args.get("before", type=int)
    if not before_id:
        return jsonify([])
    try:
        _msg_svc.assert_room_access(current_user.id, rid)
        msgs = _msg_svc.load_room_messages(rid, before_id=before_id)
        return jsonify([m.to_dict() for m in msgs])
    except AppError as e:
        return api_error(e)


@chat_bp.route("/delete/<int:mid>", methods=["POST"])
@login_required
def delete_message(mid):
    try:
        _msg_svc.soft_delete(mid, caller_id=current_user.id)
        try:
            from app import socketio

            msg = Message.query.get(mid)
            if msg:
                socketio.emit(
                    "message_deleted",
                    {"id": mid},
                    room=f"chat_{msg.room_id}",
                )
        except Exception:
            pass
        return api_ok(message="Message deleted")
    except AppError as e:
        return api_error(e)


@chat_bp.route("/edit/<int:mid>", methods=["POST"])
@login_required
def edit_message(mid):
    try:
        new_content = request.form.get("content", "").strip()
        msg = _msg_svc.edit(mid, caller_id=current_user.id, new_content=new_content)
        d = msg.to_dict()
        try:
            from app import socketio

            socketio.emit("message_edited", d, room=f"chat_{msg.room_id}")
        except Exception:
            pass
        return api_ok(d)
    except AppError as e:
        return api_error(e)


@chat_bp.route("/unread-count")
@login_required
def unread_count():
    return jsonify({"count": _msg_svc.unread_count(current_user.id)})


# ── SocketIO events ────────────────────────────────────────────────────────────
def register_socketio_events(sio):
    @sio.on("connect")
    def on_connect():
        if current_user.is_authenticated:
            join_room(f"user_{current_user.id}")

    @sio.on("join_chat")
    def on_join_chat(data):
        if not current_user.is_authenticated:
            return
        oid = data.get("other_id")
        if oid:
            room = _room_key(current_user.id, int(oid))
            join_room(f"chat_{room}")
            emit(
                "user_online",
                {"user_id": current_user.id},
                room=f"user_{oid}",
            )

    @sio.on("join_room_chat")
    def on_join_room_chat(data):
        if not current_user.is_authenticated:
            return
        rid = data.get("room_id")
        if not rid:
            return
        try:
            _msg_svc.assert_room_access(current_user.id, int(rid))
            join_room(f"chat_{rgrp_room_key(int(rid))}")
        except Exception:
            pass

    @sio.on("leave_chat")
    def on_leave_chat(data):
        if not current_user.is_authenticated:
            return
        oid = data.get("other_id")
        if oid:
            leave_room(f"chat_{_room_key(current_user.id, int(oid))}")

    @sio.on("typing")
    def on_typing(data):
        if not current_user.is_authenticated:
            return
        oid = data.get("other_id")
        if oid:
            room = _room_key(current_user.id, int(oid))
            emit(
                "typing",
                {"user_id": current_user.id, "name": current_user.full_name},
                room=f"chat_{room}",
                include_self=False,
            )

    @sio.on("mark_read")
    def on_mark_read(data):
        if not current_user.is_authenticated:
            return
        room_id = data.get("room_id")
        if not room_id or not str(room_id).startswith("dm_"):
            return
        _msg_svc.mark_read(room_id, current_user.id)
        emit(
            "read_receipt",
            {"room_id": room_id, "reader_id": current_user.id},
            room=f"chat_{room_id}",
            include_self=False,
        )
