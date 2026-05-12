"""
MessageService
--------------
All chat message operations: send, soft-delete, edit, paginated load.

SAFETY RULES:
  1. sender_id and receiver_id validated before insert
  2. File size capped at MAX_FILE_BYTES
  3. Soft delete: is_deleted=True, content cleared (GDPR-safe)
  4. Edit: only sender can edit; file messages cannot be edited
  5. Pagination: last N messages per room, no full-table scans
"""
import os
import uuid
from typing import Optional

from models import db, User, Message, Room, RoomTenant, now_utc
from services.base import BaseService
from utils.errors import (
    ValidationError, NotFoundError, PermissionError_
)

MAX_FILE_BYTES  = 10 * 1024 * 1024   # 10 MB
PAGE_SIZE       = 50                  # messages per page

IMAGE_EXTS = {"png", "jpg", "jpeg", "gif", "webp"}
VIDEO_EXTS = {"mp4", "mov", "avi", "webm"}
AUDIO_EXTS = {"mp3", "ogg", "wav", "m4a", "aac"}


def _room_key(uid1: int, uid2: int) -> str:
    """Stable, deterministic conversation key."""
    return f"dm_{min(uid1, uid2)}_{max(uid1, uid2)}"


def rgrp_room_key(room_id: int) -> str:
    """Shared room chat channel key (linked to rooms.id)."""
    return f"rgrp_{room_id}"


def parse_rgrp_room(room_id_str: Optional[str]) -> Optional[int]:
    if not room_id_str or not room_id_str.startswith("rgrp_"):
        return None
    try:
        return int(room_id_str.split("_", 1)[1])
    except (ValueError, IndexError):
        return None


def _classify_ext(ext: str) -> str:
    e = ext.lower().lstrip(".")
    if e in IMAGE_EXTS:  return "image"
    if e in VIDEO_EXTS:  return "video"
    if e in AUDIO_EXTS:  return "audio"
    return "file"


class MessageService(BaseService):

    # ── Send text message ──────────────────────────────────────────────────────
    def send_text(self, sender_id: int, receiver_id: int, content: str) -> Message:
        """Send a plain text message. Validates both user IDs."""
        self._guard_sender(sender_id)
        self._guard_receiver(receiver_id)

        if not content or not str(content).strip():
            raise ValidationError("Message content must not be empty")
        if len(content) > 4000:
            raise ValidationError("Message must be at most 4,000 characters")

        room = _room_key(sender_id, receiver_id)
        msg  = Message(
            sender_id   = sender_id,
            receiver_id = receiver_id,
            room_id     = room,
            content     = content.strip(),
        )
        with self.transaction(f"send_text s={sender_id} r={receiver_id}"):
            db.session.add(msg)
            db.session.flush()

        self.log.info(
            "Message sent",
            extra={"msg_id": msg.id, "sender_id": sender_id,
                   "receiver_id": receiver_id, "type": "text"},
        )
        return msg

    # ── Send file / media ──────────────────────────────────────────────────────
    def send_file(
        self,
        sender_id:   int,
        receiver_id: int,
        file_obj,             # werkzeug FileStorage
        upload_dir:  str,
        allowed_exts: set,
        content:     str = None,   # optional caption
    ) -> Message:
        """
        Save the uploaded file and create a message record.
        Validates: size, extension, sender/receiver.
        """
        self._guard_sender(sender_id)
        self._guard_receiver(receiver_id)

        if not file_obj or not file_obj.filename:
            raise ValidationError("No file provided")

        # Size check (seek to end)
        file_obj.seek(0, 2)
        size = file_obj.tell()
        file_obj.seek(0)
        if size > MAX_FILE_BYTES:
            raise ValidationError(f"File too large — max {MAX_FILE_BYTES // (1024*1024)} MB")

        # Extension check
        ext = file_obj.filename.rsplit(".", 1)[-1].lower() if "." in file_obj.filename else ""
        if ext not in allowed_exts:
            raise ValidationError(f"File type '.{ext}' is not allowed")

        # Save to disk
        safe_name  = f"{uuid.uuid4().hex}.{ext}"
        os.makedirs(upload_dir, exist_ok=True)
        dest_path  = os.path.join(upload_dir, safe_name)
        file_obj.save(dest_path)

        file_url   = f"/static/uploads/{safe_name}"
        file_type  = _classify_ext(ext)

        room = _room_key(sender_id, receiver_id)
        msg  = Message(
            sender_id   = sender_id,
            receiver_id = receiver_id,
            room_id     = room,
            content     = content.strip() if content and content.strip() else None,
            file_url    = file_url,
            file_name   = file_obj.filename[:255],
            file_type   = file_type,
            file_size   = size,
        )
        with self.transaction(f"send_file s={sender_id} r={receiver_id}"):
            db.session.add(msg)
            db.session.flush()

        self.log.info(
            "File message sent",
            extra={
                "msg_id": msg.id, "sender_id": sender_id,
                "receiver_id": receiver_id, "file_type": file_type,
                "file_size": size, "ext": ext,
            },
        )
        return msg

    # ── Soft delete ────────────────────────────────────────────────────────────
    def soft_delete(self, message_id: int, caller_id: int) -> bool:
        """
        Mark message as deleted; clear content (privacy).
        Caller must be the sender OR an admin.
        Physical file is also removed if present.
        """
        msg = Message.query.get(message_id)
        if not msg:
            raise NotFoundError("Message", message_id)
        if msg.is_deleted:
            return True   # idempotent

        caller = User.query.get(caller_id)
        if not caller:
            raise NotFoundError("User", caller_id)

        allowed = msg.sender_id == caller_id or caller.role == "admin"
        if not allowed and msg.room_id and msg.room_id.startswith("rgrp_"):
            rid = parse_rgrp_room(msg.room_id)
            if rid:
                room = Room.query.get(rid)
                if room and caller.role == "owner" and room.owner_id == caller_id:
                    allowed = True
        if not allowed:
            raise PermissionError_(
                f"User {caller_id} cannot delete message {message_id}",
                msg_id=message_id, caller_id=caller_id,
            )

        self.log.info(
            "Message soft-deleted",
            extra={"msg_id": message_id, "caller_id": caller_id,
                   "room_id": msg.room_id},
        )

        # Remove physical file
        file_url = msg.file_url
        with self.transaction(f"soft_delete_msg id={message_id}"):
            msg.is_deleted = True
            msg.content    = None   # clear content for privacy
            msg.file_url   = None   # unlink from response

        # File removal is outside the transaction (non-critical)
        if file_url:
            self._remove_file(file_url)

        return True

    # ── Edit message ───────────────────────────────────────────────────────────
    def edit(self, message_id: int, caller_id: int, new_content: str) -> Message:
        """
        Edit the text of a message.
        Only the sender may edit; file messages cannot be edited.
        """
        msg = Message.query.get(message_id)
        if not msg or msg.is_deleted:
            raise NotFoundError("Message", message_id)

        allowed_edit = msg.sender_id == caller_id
        if not allowed_edit and msg.room_id and msg.room_id.startswith("rgrp_"):
            rid = parse_rgrp_room(msg.room_id)
            if rid:
                room = Room.query.get(rid)
                if room and caller.role == "owner" and room.owner_id == caller_id:
                    allowed_edit = True
        if not allowed_edit:
            raise PermissionError_(
                f"User {caller_id} cannot edit message {message_id}"
            )
        if msg.file_url:
            raise ValidationError("File messages cannot be edited")
        if not new_content or not new_content.strip():
            raise ValidationError("Edited content must not be empty")
        if len(new_content) > 4000:
            raise ValidationError("Message must be at most 4,000 characters")

        with self.transaction(f"edit_msg id={message_id}"):
            msg.content = new_content.strip()

        return msg

    # ── Mark messages read ─────────────────────────────────────────────────────
    def mark_read(self, room_id: str, reader_id: int) -> int:
        """Mark all unread messages in a room as read. Returns count updated."""
        count = (
            Message.query
            .filter_by(room_id=room_id, receiver_id=reader_id,
                       is_read=False, is_deleted=False)
            .update({"is_read": True}, synchronize_session="fetch")
        )
        if count:
            self.safe_commit(f"mark_read room={room_id} reader={reader_id}")
        return count

    # ── Load conversation (paginated) ──────────────────────────────────────────
    def load_conversation(
        self,
        uid1:      int,
        uid2:      int,
        before_id: Optional[int] = None,
        limit:     int = PAGE_SIZE,
    ) -> list:
        """
        Load up to `limit` messages for a conversation, newest-first,
        optionally paginating with `before_id`.
        Returns messages in chronological order (reversed after query).
        """
        if not uid1 or not uid2:
            raise ValidationError("Both user IDs are required")

        room = _room_key(uid1, uid2)
        q    = Message.query.filter_by(room_id=room, is_deleted=False)
        if before_id:
            q = q.filter(Message.id < before_id)

        msgs = q.order_by(Message.created_at.desc()).limit(limit).all()
        return list(reversed(msgs))

    # ── Unread count ───────────────────────────────────────────────────────────
    def unread_count(self, user_id: int) -> int:
        return (
            Message.query
            .filter_by(receiver_id=user_id, is_read=False, is_deleted=False)
            .count()
        )

    # ── Shared room chat (roommates + owner) ──────────────────────────────────
    def _guard_room_chat(self, user_id: int, room: Room) -> User:
        u = User.query.get(user_id)
        if not u:
            raise NotFoundError("User", user_id)
        if not u.is_active:
            raise PermissionError_(f"Account {user_id} is deactivated")
        if u.role == "admin":
            return u
        if u.role == "owner" and room.owner_id == user_id:
            return u
        if u.role == "tenant":
            if not u.is_verified:
                raise PermissionError_(
                    "Complete verification to access property chat",
                )
            rt = RoomTenant.query.filter_by(
                room_id=room.id, tenant_id=user_id, is_active=True
            ).first()
            if not rt:
                raise PermissionError_("You are not assigned to this room")
            return u
        raise PermissionError_("Chat not allowed")

    def send_room_text(self, sender_id: int, room_id: int, content: str) -> Message:
        room = Room.query.filter_by(id=room_id, is_active=True).first()
        if not room:
            raise NotFoundError("Room", room_id)
        self._guard_room_chat(sender_id, room)

        if not content or not str(content).strip():
            raise ValidationError("Message content must not be empty")
        if len(content) > 4000:
            raise ValidationError("Message must be at most 4,000 characters")

        key = rgrp_room_key(room.id)
        msg = Message(
            sender_id=sender_id,
            receiver_id=None,
            property_id=room.property_id,
            room_id=key,
            content=content.strip(),
        )
        with self.transaction(f"room_text room={room_id} sender={sender_id}"):
            db.session.add(msg)
            db.session.flush()

        self.log.info(
            "Room message sent",
            extra={"msg_id": msg.id, "room_id": room_id, "sender_id": sender_id},
        )
        return msg

    def send_room_file(
        self,
        sender_id: int,
        room_id: int,
        file_obj,
        upload_dir: str,
        allowed_exts: set,
        content: Optional[str] = None,
    ) -> Message:
        room = Room.query.filter_by(id=room_id, is_active=True).first()
        if not room:
            raise NotFoundError("Room", room_id)
        self._guard_room_chat(sender_id, room)

        if not file_obj or not file_obj.filename:
            raise ValidationError("No file provided")

        file_obj.seek(0, 2)
        size = file_obj.tell()
        file_obj.seek(0)
        if size > MAX_FILE_BYTES:
            raise ValidationError(f"File too large — max {MAX_FILE_BYTES // (1024*1024)} MB")

        ext = file_obj.filename.rsplit(".", 1)[-1].lower() if "." in file_obj.filename else ""
        if ext not in allowed_exts:
            raise ValidationError(f"File type '.{ext}' is not allowed")

        safe_name = f"{uuid.uuid4().hex}.{ext}"
        os.makedirs(upload_dir, exist_ok=True)
        dest_path = os.path.join(upload_dir, safe_name)
        file_obj.save(dest_path)

        file_url = f"/static/uploads/{safe_name}"
        file_type = _classify_ext(ext)

        key = rgrp_room_key(room.id)
        msg = Message(
            sender_id=sender_id,
            receiver_id=None,
            property_id=room.property_id,
            room_id=key,
            content=content.strip() if content and content.strip() else None,
            file_url=file_url,
            file_name=file_obj.filename[:255],
            file_type=file_type,
            file_size=size,
        )
        with self.transaction(f"room_file room={room_id} sender={sender_id}"):
            db.session.add(msg)
            db.session.flush()

        self.log.info(
            "Room file sent",
            extra={
                "msg_id": msg.id,
                "room_id": room_id,
                "sender_id": sender_id,
                "file_type": file_type,
            },
        )
        return msg

    def load_room_messages(
        self,
        room_id: int,
        before_id: Optional[int] = None,
        limit: int = PAGE_SIZE,
    ) -> list:
        key = rgrp_room_key(room_id)
        q = Message.query.filter_by(room_id=key, is_deleted=False)
        if before_id:
            q = q.filter(Message.id < before_id)
        msgs = q.order_by(Message.created_at.desc()).limit(limit).all()
        return list(reversed(msgs))

    def assert_room_access(self, user_id: int, room_id: int) -> Room:
        room = Room.query.filter_by(id=room_id, is_active=True).first()
        if not room:
            raise NotFoundError("Room", room_id)
        self._guard_room_chat(user_id, room)
        return room

    # ── Guards ─────────────────────────────────────────────────────────────────
    def _guard_sender(self, sender_id: int):
        if not sender_id:
            raise ValidationError("sender_id is required and must not be None")
        u = User.query.get(sender_id)
        if not u:
            raise NotFoundError("Sender", sender_id)
        if not u.is_active:
            raise PermissionError_(f"Sender account {sender_id} is deactivated")

    def _guard_receiver(self, receiver_id: int):
        if not receiver_id:
            raise ValidationError("receiver_id is required and must not be None")
        u = User.query.get(receiver_id)
        if not u:
            raise NotFoundError("Receiver", receiver_id)

    def _remove_file(self, file_url: str):
        try:
            from flask import current_app
            local = os.path.join(current_app.root_path, file_url.lstrip("/"))
            if os.path.exists(local):
                os.remove(local)
                self.log.info("File removed", extra={"path": local})
        except Exception as exc:
            self.log.warning("File removal failed (non-fatal)", extra={"reason": str(exc)})
