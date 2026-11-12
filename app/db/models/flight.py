"""The ``flights`` table (Rule 2 and Rule 5).

Guards the migration puts on this table:

* ``flights_enforce_lifecycle`` (deferred constraint trigger): every new flight is
  ``planned`` with its ``flight_created`` audit row from the same transaction; every status
  change is an edge in ``flight_transitions`` with a matching audit row from the same
  transaction; and **entering ``started`` requires a stored permission, an acknowledged
  mission checksum, and a permission that is still in force and covers the mission's
  duration**. The mission and permission columns are write-once.
* At most one *live* flight per drone and per order (partial unique indexes), so a drone
  cannot be double-booked and an order cannot be flown twice at once.
* ``CHECK`` constraints that mirror the domain's per-status field rules.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Double,
    ForeignKey,
    Index,
    Text,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.domain.models import FlightStatus

S = FlightStatus


def _in(*statuses: FlightStatus) -> str:
    return ", ".join(f"'{status.value}'" for status in statuses)


_ALL = _in(*FlightStatus)
_PERMISSION_REQUIRED = _in(
    S.PERMISSION_GRANTED,
    S.MISSION_UPLOADED,
    S.UPLOAD_FAILED,
    S.STARTED,
    S.PAUSED,
    S.COMPLETED,
    S.ABORTED,
)
_ACK_REQUIRED = _in(S.MISSION_UPLOADED, S.STARTED, S.PAUSED, S.COMPLETED, S.ABORTED)
_ACK_FORBIDDEN = _in(S.PLANNED, S.PERMISSION_GRANTED, S.PERMISSION_DENIED, S.UPLOAD_FAILED)
_STARTED = _in(S.STARTED, S.PAUSED, S.COMPLETED, S.ABORTED)
_WITH_REASON = _in(S.UPLOAD_FAILED, S.CANCELLED, S.ABORTED)
_LIVE = _in(S.PLANNED, S.PERMISSION_GRANTED, S.MISSION_UPLOADED, S.STARTED, S.PAUSED)


class FlightRecord(Base):
    __tablename__ = "flights"
    __table_args__ = (
        CheckConstraint(f"status IN ({_ALL})", name="status_valid"),
        CheckConstraint("mission_estimated_duration_s > 0", name="mission_duration_positive"),
        CheckConstraint(
            "(permission_id IS NULL) = (permission_issuer IS NULL)"
            " AND (permission_id IS NULL) = (permission_issued_at IS NULL)"
            " AND (permission_id IS NULL) = (permission_valid_from IS NULL)"
            " AND (permission_id IS NULL) = (permission_valid_until IS NULL)",
            name="permission_fields_together",
        ),
        CheckConstraint(
            f"status NOT IN ({_PERMISSION_REQUIRED}) OR permission_id IS NOT NULL",
            name="permission_required_once_granted",
        ),
        CheckConstraint(
            f"status NOT IN ({_in(S.PLANNED, S.PERMISSION_DENIED)}) OR permission_id IS NULL",
            name="no_permission_before_grant",
        ),
        CheckConstraint(
            f"status NOT IN ({_ACK_REQUIRED}) OR acknowledged_checksum IS NOT NULL",
            name="ack_required_once_uploaded",
        ),
        CheckConstraint(
            f"status NOT IN ({_ACK_FORBIDDEN}) OR acknowledged_checksum IS NULL",
            name="ack_forbidden_before_upload",
        ),
        CheckConstraint(
            "acknowledged_checksum IS NULL OR acknowledged_checksum = mission_checksum",
            name="ack_matches_mission",
        ),
        CheckConstraint(
            f"(status IN ({_STARTED})) = (started_at IS NOT NULL)",
            name="started_at_matches_status",
        ),
        CheckConstraint(
            f"(status IN ({_WITH_REASON})) = (failure_reason IS NOT NULL)",
            name="reason_matches_status",
        ),
        CheckConstraint(
            "(denial_code IS NULL) = (denial_reason IS NULL)", name="denial_fields_together"
        ),
        CheckConstraint(
            f"(status = '{S.PERMISSION_DENIED.value}') = (denial_code IS NOT NULL)",
            name="denial_matches_status",
        ),
        Index("ix_flights_order_id", "order_id"),
        Index("ix_flights_status", "status"),
        # At most one live flight per drone and per order.
        Index(
            "uq_flights_live_per_drone",
            "drone_id",
            unique=True,
            postgresql_where=text(f"status IN ({_LIVE})"),
        ),
        Index(
            "uq_flights_live_per_order",
            "order_id",
            unique=True,
            postgresql_where=text(f"status IN ({_LIVE})"),
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    order_id: Mapped[UUID] = mapped_column(Uuid, ForeignKey("orders.id", ondelete="RESTRICT"))
    drone_id: Mapped[UUID] = mapped_column(Uuid, ForeignKey("drones.id", ondelete="RESTRICT"))
    status: Mapped[str] = mapped_column(Text)
    mission: Mapped[dict[str, Any]] = mapped_column(JSONB)
    mission_checksum: Mapped[str] = mapped_column(Text)
    mission_estimated_duration_s: Mapped[float] = mapped_column(Double)
    permission_id: Mapped[str | None] = mapped_column(Text)
    permission_issuer: Mapped[str | None] = mapped_column(Text)
    permission_issued_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    permission_valid_from: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    permission_valid_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    denial_code: Mapped[str | None] = mapped_column(Text)
    denial_reason: Mapped[str | None] = mapped_column(Text)
    acknowledged_checksum: Mapped[str | None] = mapped_column(Text)
    failure_reason: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
