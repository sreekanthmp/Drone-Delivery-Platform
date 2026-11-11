"""The ``orders`` table.

Status is plain ``TEXT`` guarded by a ``CHECK`` (easier to migrate than a Postgres
ENUM). Locations are PostGIS ``geography(Point, 4326)``; note PostGIS orders
coordinates as (longitude, latitude).

``drone_id`` is a plain UUID for now: the ``drones`` table (and this foreign key)
arrive with the dispatch phase.

Lifecycle rules for this table (valid edges, mandatory audit row) are enforced by
the ``orders_enforce_lifecycle`` constraint trigger created in the migration.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from geoalchemy2 import Geography
from sqlalchemy import CheckConstraint, DateTime, Double, Index, Text, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.domain.models import OrderStatus

_STATUS_VALUES = ", ".join(f"'{status.value}'" for status in OrderStatus)


class OrderRecord(Base):
    __tablename__ = "orders"
    __table_args__ = (
        UniqueConstraint("client_id", "external_order_id", name="uq_orders_client_external"),
        CheckConstraint("btrim(client_id) <> ''", name="client_id_not_blank"),
        CheckConstraint("btrim(external_order_id) <> ''", name="external_order_id_not_blank"),
        CheckConstraint("package_weight_kg > 0", name="package_weight_positive"),
        CheckConstraint(f"status IN ({_STATUS_VALUES})", name="status_valid"),
        Index("ix_orders_status", "status"),
        Index("ix_orders_pickup", "pickup", postgresql_using="gist"),
        Index("ix_orders_dropoff", "dropoff", postgresql_using="gist"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    client_id: Mapped[str] = mapped_column(Text)
    external_order_id: Mapped[str] = mapped_column(Text)
    pickup: Mapped[Any] = mapped_column(Geography("POINT", srid=4326, spatial_index=False))
    dropoff: Mapped[Any] = mapped_column(Geography("POINT", srid=4326, spatial_index=False))
    package_weight_kg: Mapped[float] = mapped_column(Double)
    package_description: Mapped[str] = mapped_column(Text, server_default="")
    status: Mapped[str] = mapped_column(Text)
    drone_id: Mapped[UUID | None] = mapped_column(Uuid)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
