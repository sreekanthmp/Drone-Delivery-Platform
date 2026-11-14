"""Webhook delivery repository (Phase 6).

The queue of notifications owed to partners. ``add`` is called only by the order store, inside
the transaction that writes the order's audit event. The sender claims, and records the outcome
of, deliveries through ``claim_due`` and ``apply``. Attempts are insert-only (and the database
rejects updating or deleting them). Nothing here reads or writes an order's status.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, cast
from uuid import UUID

from sqlalchemy import CursorResult, exists, insert, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import Row
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.db.models import WebhookAttemptRecord, WebhookDeliveryRecord
from app.domain.models import (
    AttemptOutcome,
    AuditEvent,
    DeliveryAttempt,
    DeliveryStatus,
    WebhookDelivery,
)

_D = WebhookDeliveryRecord


def _to_delivery(row: Row[Any] | WebhookDeliveryRecord) -> WebhookDelivery:
    return WebhookDelivery(
        event_id=row.event_id,
        client_id=row.client_id,
        order_id=row.order_id,
        status=DeliveryStatus(row.status),
        attempts=row.attempts,
        round_start=row.round_start,
        next_attempt_at=row.next_attempt_at,
        created_at=row.created_at,
        updated_at=row.updated_at,
        seq=row.seq,
        lease_until=row.lease_until,
        last_status_code=row.last_status_code,
        last_error=row.last_error,
        delivered_at=row.delivered_at,
    )


def _to_attempt(row: WebhookAttemptRecord) -> DeliveryAttempt:
    return DeliveryAttempt(
        event_id=row.event_id,
        attempt_no=row.attempt_no,
        started_at=row.started_at,
        duration_ms=row.duration_ms,
        outcome=AttemptOutcome(row.outcome),
        status_code=row.status_code,
        error=row.error,
    )


class SqlAlchemyWebhookStore:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, event: AuditEvent, client_id: str) -> bool:
        delivery = WebhookDelivery.queue(event, client_id)
        inserted = await self._session.scalar(
            pg_insert(_D)
            .values(
                event_id=delivery.event_id,
                client_id=delivery.client_id,
                order_id=delivery.order_id,
                status=delivery.status.value,
                attempts=delivery.attempts,
                round_start=delivery.round_start,
                next_attempt_at=delivery.next_attempt_at,
                created_at=delivery.created_at,
                updated_at=delivery.updated_at,
            )
            .on_conflict_do_nothing(index_elements=["event_id"])
            .returning(_D.event_id)
        )
        return inserted is not None

    async def claim_due(
        self, *, now: datetime, lease_until: datetime, limit: int
    ) -> list[WebhookDelivery]:
        earlier = aliased(_D)
        # An order's updates go out one at a time, oldest first: while an earlier delivery of
        # the same order is still pending (waiting, leased or retrying), the next one waits.
        blocked = exists().where(
            earlier.order_id == _D.order_id,
            earlier.client_id == _D.client_id,
            earlier.status == DeliveryStatus.PENDING.value,
            earlier.seq < _D.seq,
        )
        due = (
            select(_D.event_id)
            .where(
                _D.status == DeliveryStatus.PENDING.value,
                _D.next_attempt_at <= now,
                or_(_D.lease_until.is_(None), _D.lease_until <= now),
                ~blocked,
            )
            .order_by(_D.seq)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        ids = list((await self._session.scalars(due)).all())
        if not ids:
            return []
        rows = await self._session.execute(
            update(_D)
            .where(_D.event_id.in_(ids))
            .values(lease_until=lease_until, updated_at=now)
            .returning(*_D.__table__.c)
            .execution_options(synchronize_session=False)
        )
        return sorted((_to_delivery(row) for row in rows), key=lambda d: d.seq or 0)

    async def apply(
        self, before: WebhookDelivery, after: WebhookDelivery, attempt: DeliveryAttempt | None
    ) -> bool:
        outcome = await self._session.execute(
            update(_D)
            .where(
                _D.event_id == before.event_id,
                _D.status == before.status.value,
                _D.attempts == before.attempts,
            )
            .values(
                status=after.status.value,
                attempts=after.attempts,
                round_start=after.round_start,
                next_attempt_at=after.next_attempt_at,
                lease_until=after.lease_until,
                last_status_code=after.last_status_code,
                last_error=after.last_error,
                delivered_at=after.delivered_at,
                updated_at=after.updated_at,
            )
            .execution_options(synchronize_session=False)
        )
        if cast("CursorResult[Any]", outcome).rowcount != 1:
            return False
        if attempt is not None:
            await self._session.execute(
                insert(WebhookAttemptRecord).values(
                    event_id=attempt.event_id,
                    attempt_no=attempt.attempt_no,
                    started_at=attempt.started_at,
                    duration_ms=attempt.duration_ms,
                    outcome=attempt.outcome.value,
                    status_code=attempt.status_code,
                    error=attempt.error,
                )
            )
        return True

    async def get(self, event_id: UUID) -> WebhookDelivery | None:
        record = await self._session.get(_D, event_id)
        return None if record is None else _to_delivery(record)

    async def list_recent(
        self,
        *,
        status: DeliveryStatus | None = None,
        client_id: str | None = None,
        limit: int = 100,
    ) -> list[WebhookDelivery]:
        statement = select(_D).order_by(_D.seq.desc()).limit(limit)
        if status is not None:
            statement = statement.where(_D.status == status.value)
        if client_id is not None:
            statement = statement.where(_D.client_id == client_id)
        return [_to_delivery(row) for row in await self._session.scalars(statement)]

    async def list_for_order(self, order_id: UUID) -> list[WebhookDelivery]:
        rows = await self._session.scalars(
            select(_D).where(_D.order_id == order_id).order_by(_D.seq)
        )
        return [_to_delivery(row) for row in rows]

    async def attempts_for(self, event_id: UUID) -> list[DeliveryAttempt]:
        rows = await self._session.scalars(
            select(WebhookAttemptRecord)
            .where(WebhookAttemptRecord.event_id == event_id)
            .order_by(WebhookAttemptRecord.attempt_no)
        )
        return [_to_attempt(row) for row in rows]
