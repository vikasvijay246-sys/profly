"""
RoomService
-----------
Room lifecycle: create, edit, soft-deactivate, tenant assignment.

SAFETY RULES:
  1. max_capacity strictly 1–4, enforced here AND in CheckConstraint
  2. Cannot shrink capacity below current occupancy
  3. Cannot delete a room with active tenants
  4. Duplicate room_number per property prevented
  5. Tenant assignment: full guard chain before any DB write
"""
from models import db, User, Property, Room, RoomTenant, PropertyTenant, now_utc
from services.base import BaseService
from utils.errors import (
    ValidationError, NotFoundError, ConflictError, CapacityError, PermissionError_
)


class RoomService(BaseService):

    MAX_CAPACITY = 4

    # ── Create ─────────────────────────────────────────────────────────────────
    def create(self, data: dict, owner_id: int) -> Room:
        """
        data = {"room_number": str, "max_capacity": int,
                "property_id": int|None, "description": str|None, "floor": str|None}
        """
        self._verify_owner(owner_id)
        room_number  = data["room_number"]
        max_capacity = data["max_capacity"]   # already validated 1–4

        if max_capacity > self.MAX_CAPACITY:
            raise ValidationError(f"max_capacity cannot exceed {self.MAX_CAPACITY}")

        prop_id = data.get("property_id")
        if prop_id:
            self._verify_property(prop_id, owner_id)

        # Duplicate guard
        dup = Room.query.filter_by(
            room_number=room_number,
            owner_id=owner_id,
            is_active=True,
        )
        if prop_id:
            dup = dup.filter_by(property_id=prop_id)
        if dup.first():
            raise ConflictError(
                f"Room '{room_number}' already exists for this property",
                room_number=room_number, property_id=prop_id,
            )

        room = Room(
            room_number  = room_number,
            max_capacity = max_capacity,
            property_id  = prop_id,
            owner_id     = owner_id,
            description  = data.get("description"),
            floor        = data.get("floor"),
            is_active    = True,
        )
        with self.transaction(f"create_room {room_number} owner={owner_id}"):
            db.session.add(room)
            db.session.flush()

        self.log.info("Room created", extra={"room_id": room.id,
                       "room_number": room_number, "owner_id": owner_id,
                       "max_capacity": max_capacity})
        return room

    # ── Edit ───────────────────────────────────────────────────────────────────
    def edit(self, room_id: int, owner_id: int, data: dict) -> Room:
        room = self._get_room(room_id, owner_id)

        new_cap = data.get("max_capacity")
        if new_cap is not None:
            new_cap = int(new_cap)
            if not (1 <= new_cap <= self.MAX_CAPACITY):
                raise ValidationError(f"max_capacity must be 1–{self.MAX_CAPACITY}")
            occ = room.get_occupancy()
            if new_cap < occ:
                raise ValidationError(
                    f"Cannot reduce capacity to {new_cap}: "
                    f"room currently has {occ} active tenants"
                )
            room.max_capacity = new_cap

        if "description" in data:
            room.description = (data["description"] or "")[:200]
        if "floor" in data:
            room.floor = (data["floor"] or "")[:20]
        if "property_id" in data and data["property_id"]:
            self._verify_property(data["property_id"], owner_id)
            room.property_id = data["property_id"]

        room.updated_at = now_utc()
        self.safe_commit(f"edit_room id={room_id}")
        self.log.info("Room edited", extra={"room_id": room_id, "owner_id": owner_id})
        return room

    # ── Soft deactivate ────────────────────────────────────────────────────────
    def deactivate(self, room_id: int, owner_id: int) -> bool:
        room = self._get_room(room_id, owner_id)
        occ  = room.get_occupancy()
        if occ > 0:
            raise ConflictError(
                f"Cannot deactivate Room {room.room_number}: "
                f"{occ} tenant(s) still assigned. Remove them first.",
                room_id=room_id,
            )
        with self.transaction(f"deactivate_room id={room_id}"):
            room.is_active  = False
            room.updated_at = now_utc()

        self.log.info("Room deactivated", extra={"room_id": room_id, "owner_id": owner_id})
        return True

    # ── Assign tenant ──────────────────────────────────────────────────────────
    def assign_tenant(self, room_id: int, tenant_id: int, owner_id: int) -> tuple:
        """
        Returns (ok: bool, message: str, RoomTenant|None).
        Never raises — returns False with clear message for UI.
        """
        try:
            rt = self._assign(room_id, tenant_id, owner_id)
            return True, f"Assigned to Room {rt.room.room_number}.", rt
        except (ValidationError, NotFoundError, ConflictError,
                CapacityError, PermissionError_) as exc:
            return False, str(exc), None
        except Exception as exc:
            self.log.error("Unexpected error in assign_tenant",
                           extra={"room_id": room_id, "tenant_id": tenant_id,
                                  "reason": str(exc)})
            return False, "An unexpected error occurred.", None

    def _assign(self, room_id: int, tenant_id: int, owner_id: int) -> RoomTenant:
        if not tenant_id:
            raise ValidationError("tenant_id is required")

        room   = self._get_room(room_id, owner_id)
        tenant = User.query.filter_by(id=tenant_id, role="tenant",
                                      is_active=True).first()
        if not tenant:
            raise NotFoundError("Active tenant", tenant_id)

        # Capacity
        occ = room.get_occupancy()
        if occ >= room.max_capacity:
            raise CapacityError(
                f"Room {room.room_number} is full "
                f"({occ}/{room.max_capacity} tenants)",
                room_id=room_id,
            )
        if occ >= self.MAX_CAPACITY:
            raise CapacityError(f"Maximum {self.MAX_CAPACITY} tenants per room")

        # Already active?
        existing = RoomTenant.query.filter_by(
            room_id=room_id, tenant_id=tenant_id
        ).first()
        if existing:
            if existing.is_active:
                raise ConflictError(
                    f"Tenant {tenant.full_name} is already in Room {room.room_number}"
                )
            # Re-activate
            with self.transaction(f"reactivate_rt room={room_id} tenant={tenant_id}"):
                existing.is_active  = True
                existing.vacated_at = None
            self.log.info("Tenant re-assigned to room",
                          extra={"room_id": room_id, "tenant_id": tenant_id})
            return existing

        with self.transaction(f"assign_tenant room={room_id} tenant={tenant_id}"):
            rt = RoomTenant(
                room_id        = room_id,
                tenant_id      = tenant_id,
                payment_status = "not_paid",
                is_active      = True,
            )
            db.session.add(rt)

            # Sync PropertyTenant.room_number
            if room.property_id:
                pt = PropertyTenant.query.filter_by(
                    tenant_id=tenant_id,
                    property_id=room.property_id,
                ).first()
                if pt:
                    pt.room_id     = room_id
                    pt.room_number = str(room.room_number)

        self.log.info("Tenant assigned to room",
                      extra={"room_id": room_id, "tenant_id": tenant_id,
                             "room_number": room.room_number})
        return rt

    # ── Remove tenant ──────────────────────────────────────────────────────────
    def remove_tenant(self, assignment_id: int, owner_id: int) -> bool:
        rt = RoomTenant.query.get(assignment_id)
        if not rt:
            raise NotFoundError("Room assignment", assignment_id)

        # Ownership check
        self._get_room(rt.room_id, owner_id)   # raises if not owned

        with self.transaction(f"remove_tenant rt={assignment_id}"):
            rt.is_active  = False
            rt.vacated_at = now_utc()

        self.log.info("Tenant removed from room",
                      extra={"assignment_id": assignment_id,
                             "room_id": rt.room_id, "tenant_id": rt.tenant_id})
        return True

    # ── Update payment status ──────────────────────────────────────────────────
    def update_payment_status(
        self, assignment_id: int, new_status: str, owner_id: int
    ) -> RoomTenant:
        if new_status not in ("paid", "not_paid"):
            raise ValidationError("payment_status must be 'paid' or 'not_paid'")

        rt = RoomTenant.query.get(assignment_id)
        if not rt:
            raise NotFoundError("Room assignment", assignment_id)
        self._get_room(rt.room_id, owner_id)  # ownership check

        old = rt.payment_status
        rt.payment_status = new_status
        self.safe_commit(f"update_room_payment rt={assignment_id}")

        self.log.info(
            "Room payment status updated",
            extra={"assignment_id": assignment_id, "old": old, "new": new_status},
        )
        return rt

    # ── Private helpers ────────────────────────────────────────────────────────
    def _get_room(self, room_id: int, owner_id: int) -> Room:
        room = Room.query.filter_by(id=room_id, is_active=True).first()
        if not room:
            raise NotFoundError("Room", room_id)
        caller = User.query.get(owner_id)
        if caller and caller.role == "owner" and room.owner_id != owner_id:
            raise PermissionError_(
                f"Room {room_id} does not belong to owner {owner_id}"
            )
        return room

    def _verify_owner(self, owner_id: int):
        u = User.query.get(owner_id)
        if not u:
            raise NotFoundError("Owner", owner_id)
        if u.role not in ("owner", "admin"):
            raise PermissionError_("Only owners or admins can manage rooms")

    def _verify_property(self, property_id: int, owner_id: int):
        p = Property.query.filter_by(id=property_id, is_deleted=False).first()
        if not p:
            raise NotFoundError("Property", property_id)
        caller = User.query.get(owner_id)
        if caller and caller.role == "owner" and p.owner_id != owner_id:
            raise PermissionError_(
                f"Property {property_id} does not belong to owner {owner_id}"
            )
