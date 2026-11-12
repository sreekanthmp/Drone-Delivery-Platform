"""The ``drones`` table: the fleet registry.

Drone status changes are applied by the drone store as a compare-and-set on the previous
status. A drone can never be double-booked regardless: ``flights`` allows at most one live
flight per drone (a partial unique index).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from geoalchemy2 import Geography
from sqlalchemy import CheckConstraint, DateTime, Double, Index, Text, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.domain.models import DroneStatus

_STATUS_VALUES = ", ".join(f"'{status.value}'" for status in DroneStatus)


class DroneRecord(Base):
    __tablename__ = "drones"
    __table_args__ = (
        UniqueConstraint("name", name="uq_drones_name"),
        CheckConstraint("btrim(name) <> ''", name="name_not_blank"),
        CheckConstraint(f"status IN ({_STATUS_VALUES})", name="status_valid"),
        CheckConstraint("max_payload_kg > 0", name="max_payload_positive"),
        CheckConstraint("max_range_km > 0", name="max_range_positive"),
        CheckConstraint(
            "battery_percent >= 0 AND battery_percent <= 100", name="battery_percent_in_range"
        ),
        Index("ix_drones_status", "status"),
        Index("ix_drones_home", "home", postgresql_using="gist"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    name: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text)
    home: Mapped[Any] = mapped_column(Geography("POINT", srid=4326, spatial_index=False))
    max_payload_kg: Mapped[float] = mapped_column(Double)
    max_range_km: Mapped[float] = mapped_column(Double)
    battery_percent: Mapped[float] = mapped_column(Double)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
