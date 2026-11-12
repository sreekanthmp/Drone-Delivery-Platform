"""The ``audit_events`` table (Rule 5: append-only).

Rows are only ever inserted. Triggers created in the migration reject ``UPDATE``,
``DELETE`` and ``TRUNCATE``, and the foreign key to ``orders`` is ``RESTRICT``.

``seq`` gives a strict insertion order; ``occurred_at`` is when the domain event
happened and ``recorded_at`` is when it was stored.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Text,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.domain.models import AuditEventType, EventSource, FlightStatus, OrderStatus


def _in(statuses: type[OrderStatus] | type[FlightStatus]) -> str:
    return ", ".join(f"'{status.value}'" for status in statuses)


_ORDER_STATES = _in(OrderStatus)
_FLIGHT_STATES = _in(FlightStatus)
# Both vocabularies share some strings ("cancelled"); the per-type check below keeps them apart.
_ALL_STATES = ", ".join(sorted({f"'{s.value}'" for s in (*OrderStatus, *FlightStatus)}))
_TYPE_VALUES = ", ".join(f"'{event_type.value}'" for event_type in AuditEventType)
_SOURCE_VALUES = ", ".join(f"'{source.value}'" for source in EventSource)


class AuditEventRecord(Base):
    __tablename__ = "audit_events"
    __table_args__ = (
        UniqueConstraint("seq", name="uq_audit_events_seq"),
        CheckConstraint(f"event_type IN ({_TYPE_VALUES})", name="event_type_valid"),
        CheckConstraint(f"source IN ({_SOURCE_VALUES})", name="source_valid"),
        CheckConstraint(
            f"previous_state IS NULL OR previous_state IN ({_ALL_STATES})",
            name="previous_state_valid",
        ),
        CheckConstraint(
            f"new_state IS NULL OR new_state IN ({_ALL_STATES})", name="new_state_valid"
        ),
        # NB: the explicit IS NOT NULL tests are load-bearing. ``x IN (...)`` is NULL, not false,
        # when x is NULL, and a CHECK constraint accepts NULL.
        CheckConstraint(
            "(event_type = 'order_created' AND previous_state IS NULL AND new_state = 'created'"
            " AND flight_id IS NULL)"
            " OR (event_type = 'order_status_changed'"
            " AND previous_state IS NOT NULL AND new_state IS NOT NULL"
            f" AND previous_state IN ({_ORDER_STATES}) AND new_state IN ({_ORDER_STATES})"
            " AND previous_state <> new_state AND flight_id IS NULL)"
            " OR (event_type = 'flight_created' AND previous_state IS NULL"
            " AND new_state = 'planned' AND flight_id IS NOT NULL AND drone_id IS NOT NULL)"
            " OR (event_type = 'flight_status_changed'"
            " AND previous_state IS NOT NULL AND new_state IS NOT NULL"
            f" AND previous_state IN ({_FLIGHT_STATES}) AND new_state IN ({_FLIGHT_STATES})"
            " AND previous_state <> new_state AND flight_id IS NOT NULL AND drone_id IS NOT NULL)",
            name="type_matches_states",
        ),
        Index("ix_audit_events_order_id_seq", "order_id", "seq"),
        Index("ix_audit_events_flight_id_seq", "flight_id", "seq"),
        # An order enters each lifecycle state at most once (the state graph has no cycles).
        # Flights are exempt: pause and resume let a flight re-enter 'started'.
        Index(
            "uq_audit_events_order_new_state",
            "order_id",
            "new_state",
            unique=True,
            postgresql_where=text("event_type IN ('order_created', 'order_status_changed')"),
        ),
    )

    event_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    seq: Mapped[int] = mapped_column(BigInteger, Identity(always=True))
    order_id: Mapped[UUID] = mapped_column(Uuid, ForeignKey("orders.id", ondelete="RESTRICT"))
    drone_id: Mapped[UUID | None] = mapped_column(Uuid)
    flight_id: Mapped[UUID | None] = mapped_column(
        Uuid, ForeignKey("flights.id", ondelete="RESTRICT")
    )
    event_type: Mapped[str] = mapped_column(Text)
    previous_state: Mapped[str | None] = mapped_column(Text)
    new_state: Mapped[str | None] = mapped_column(Text)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    source: Mapped[str] = mapped_column(Text)
    # Attribute is not called ``metadata``: that name is reserved by SQLAlchemy's declarative base.
    event_metadata: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONB, server_default=text("'{}'::jsonb")
    )
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
