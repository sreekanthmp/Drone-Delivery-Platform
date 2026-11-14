"""The ``webhook_deliveries`` and ``webhook_attempts`` tables (Phase 6).

A delivery is the queued notification of one order audit event to its partner, identified by
that event's id, so an event can be queued at most once. ``seq`` gives insertion order, which
is the order an order's updates are sent in. An attempt is one HTTP try at a delivery; attempts
are only ever inserted (triggers in the migration reject ``UPDATE``, ``DELETE`` and
``TRUNCATE``, like the audit table).
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.domain.models import AttemptOutcome, DeliveryStatus

_STATUSES = ", ".join(f"'{s.value}'" for s in DeliveryStatus)
_OUTCOMES = ", ".join(f"'{o.value}'" for o in AttemptOutcome)


class WebhookDeliveryRecord(Base):
    __tablename__ = "webhook_deliveries"
    __table_args__ = (
        UniqueConstraint("seq", name="uq_webhook_deliveries_seq"),
        CheckConstraint(f"status IN ({_STATUSES})", name="status_valid"),
        CheckConstraint("client_id <> ''", name="client_id_not_empty"),
        CheckConstraint(
            "attempts >= 0 AND round_start >= 0 AND round_start <= attempts", name="attempts_valid"
        ),
        CheckConstraint(
            "last_status_code IS NULL OR last_status_code BETWEEN 100 AND 599",
            name="status_code_valid",
        ),
        CheckConstraint(
            "(status = 'delivered') = (delivered_at IS NOT NULL)", name="delivered_at_matches"
        ),
        CheckConstraint("lease_until IS NULL OR status = 'pending'", name="lease_only_pending"),
        Index("ix_webhook_deliveries_order_id_seq", "order_id", "seq"),
        Index("ix_webhook_deliveries_status_seq", "status", "seq"),
        Index("ix_webhook_deliveries_client_id_seq", "client_id", "seq"),
        # What the sender scans: only the pending ones, soonest due first.
        Index(
            "ix_webhook_deliveries_due",
            "next_attempt_at",
            postgresql_where=text("status = 'pending'"),
        ),
    )

    event_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("audit_events.event_id", ondelete="RESTRICT"), primary_key=True
    )
    seq: Mapped[int] = mapped_column(BigInteger, Identity(always=True))
    client_id: Mapped[str] = mapped_column(Text)
    order_id: Mapped[UUID] = mapped_column(Uuid, ForeignKey("orders.id", ondelete="RESTRICT"))
    status: Mapped[str] = mapped_column(Text)
    attempts: Mapped[int] = mapped_column(Integer)
    round_start: Mapped[int] = mapped_column(Integer)
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_status_code: Mapped[int | None] = mapped_column(Integer)
    last_error: Mapped[str | None] = mapped_column(Text)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class WebhookAttemptRecord(Base):
    __tablename__ = "webhook_attempts"
    __table_args__ = (
        UniqueConstraint("event_id", "attempt_no", name="uq_webhook_attempts_event_attempt"),
        CheckConstraint(f"outcome IN ({_OUTCOMES})", name="outcome_valid"),
        CheckConstraint("attempt_no >= 1", name="attempt_no_valid"),
        CheckConstraint("duration_ms >= 0", name="duration_valid"),
        CheckConstraint(
            "status_code IS NULL OR status_code BETWEEN 100 AND 599", name="status_code_valid"
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    event_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("webhook_deliveries.event_id", ondelete="RESTRICT")
    )
    attempt_no: Mapped[int] = mapped_column(Integer)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    duration_ms: Mapped[int] = mapped_column(Integer)
    outcome: Mapped[str] = mapped_column(Text)
    status_code: Mapped[int | None] = mapped_column(Integer)
    error: Mapped[str | None] = mapped_column(Text)
