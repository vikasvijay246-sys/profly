"""
NotificationService
-------------------
All notification creation and real-time push logic lives here.

Rule: every public method is safe to call even if SocketIO is None
(falls back gracefully without crashing).
"""
from models import db, Notification
from services.base import BaseService
from utils.errors import ValidationError


class NotificationService(BaseService):

    # ── Create & persist ───────────────────────────────────────────────────────
    def create(
        self,
        user_id:    int,
        title:      str,
        body:       str,
        notif_type: str = "general",
    ) -> Notification:
        """
        Save a notification to DB.
        Does NOT commit — caller manages the transaction so this can be
        included in a larger atomic operation.
        """
        if not user_id:
            raise ValidationError("user_id is required for notification")
        if not title or not title.strip():
            raise ValidationError("Notification title must not be empty")
        if not body or not body.strip():
            raise ValidationError("Notification body must not be empty")

        notif = Notification(
            user_id    = user_id,
            title      = title[:255],    # hard truncate to column limit
            body       = body,
            notif_type = notif_type,
        )
        db.session.add(notif)
        return notif

    # ── Create + push (atomic) ─────────────────────────────────────────────────
    def push(
        self,
        socketio,          # may be None — handled gracefully
        user_id:    int,
        title:      str,
        body:       str,
        notif_type: str = "general",
    ) -> Notification:
        """
        Save notification to DB then emit real-time event.
        Commits its own transaction.
        SocketIO failure does NOT roll back the DB record.
        """
        with self.transaction(f"push_notification user={user_id}"):
            notif = self.create(user_id, title, body, notif_type)
            # flush to get notif.id before commit
            db.session.flush()
            notif_dict = notif.to_dict()

        # Real-time emit is best-effort; never crash the caller
        self._emit_safe(socketio, "notification", notif_dict, room=f"user_{user_id}")

        self.log.info(
            "Notification sent",
            extra={"user_id": user_id, "notif_type": notif_type,
                   "notif_id": notif.id},
        )
        return notif

    # ── Bulk notify (e.g. broadcast to all tenants of an owner) ───────────────
    def broadcast(
        self,
        socketio,
        user_ids:   list,
        title:      str,
        body:       str,
        notif_type: str = "general",
    ) -> int:
        """
        Create one notification per user_id in a single transaction.
        Returns count created.
        """
        if not user_ids:
            return 0
        created = 0
        with self.transaction(f"broadcast to {len(user_ids)} users"):
            for uid in user_ids:
                if uid:
                    self.create(uid, title, body, notif_type)
                    created += 1

        # Real-time push is outside the transaction
        for uid in user_ids:
            if uid:
                self._emit_safe(
                    socketio, "notification",
                    {"title": title, "body": body, "notif_type": notif_type},
                    room=f"user_{uid}",
                )

        self.log.info("Broadcast sent", extra={"user_count": created, "notif_type": notif_type})
        return created

    # ── Mark read ──────────────────────────────────────────────────────────────
    def mark_read(self, user_id: int, notif_id: int) -> bool:
        notif = Notification.query.filter_by(id=notif_id, user_id=user_id).first()
        if not notif:
            return False
        notif.is_read = True
        self.safe_commit(f"mark_read notif={notif_id}")
        return True

    def mark_all_read(self, user_id: int) -> int:
        count = (Notification.query
                 .filter_by(user_id=user_id, is_read=False)
                 .update({"is_read": True}))
        self.safe_commit(f"mark_all_read user={user_id}")
        return count

    def unread_count(self, user_id: int) -> int:
        return Notification.query.filter_by(user_id=user_id, is_read=False).count()

    # ── Emit helper ────────────────────────────────────────────────────────────
    def _emit_safe(self, socketio, event: str, data: dict, room: str):
        if not socketio:
            return
        try:
            socketio.emit(event, data, room=room)
        except Exception as exc:
            self.log.warning(
                "SocketIO emit failed (non-fatal)",
                extra={"event": event, "room": room, "reason": str(exc)},
            )
