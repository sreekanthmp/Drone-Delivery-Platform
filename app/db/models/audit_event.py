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
from app.domain.models import AuditEventType, EventSource, OrderStatus

_STATUS_VALUES = ", ".join(f"'{status.value}'" for status in OrderStatus)
_TYPE_VALUES = ", ".join(f"'{event_type.value}'" for event_type in AuditEventType)
_SOURCE_VALUES = ", ".join(f"'{source.value}'" for source in EventSource)


class AuditEventRecord(Base):
    __tablename__ = "audit_events"
    __table_args__ = (
        UniqueConstraint("seq", name="uq_audit_events_seq"),
        CheckConstraint(f"event_type IN ({_TYPE_VALUES})", name="event_type_valid"),
        CheckConstraint(f"source IN ({_SOURCE_VALUES})", name="source_valid"),
        CheckConstraint(
            f"previous_state IS NULL OR previous_state IN ({_STATUS_VALUES})",
            name="previous_state_valid",
        ),
        CheckConstraint(
            f"new_state IS NULL OR new_state IN ({_STATUS_VALUES})", name="new_state_valid"
        ),
        CheckConstraint(
            "(event_type = 'order_created' AND previous_state IS NULL AND new_state = 'created')"
            " OR (event_type = 'order_status_changed' AND previous_state IS NOT NULL"
            " AND new_state IS NOT NULL AND previous_state <> new_state)",
            name="type_matches_states",
        ),
        Index("ix_audit_events_order_id_seq", "order_id", "seq"),
        # An order enters each lifecycle state at most once (the state graph has no cycles).
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
