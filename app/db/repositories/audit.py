"""Audit log repository (Rule 5).

Insert and read only. There is deliberately no update or delete method here, and
the database rejects them too (see the migration's triggers).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from uuid import UUID

from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AuditEventRecord
from app.domain.models import AuditEvent, AuditEventType, EventSource, OrderStatus


def _thaw(value: object) -> Any:
    """Turn the event's frozen metadata (mappingproxy/tuple) back into JSON-serialisable types."""
    if isinstance(value, Mapping):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_thaw(item) for item in value]
    return value


def _to_domain(record: AuditEventRecord) -> AuditEvent:
    return AuditEvent(
        event_id=record.event_id,
        order_id=record.order_id,
        event_type=AuditEventType(record.event_type),
        previous_state=None
        if record.previous_state is None
        else OrderStatus(record.previous_state),
        new_state=None if record.new_state is None else OrderStatus(record.new_state),
        occurred_at=record.occurred_at,
        source=EventSource(record.source),
        drone_id=record.drone_id,
        metadata=record.event_metadata,
    )


class SqlAlchemyAuditLog:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def append(self, event: AuditEvent) -> None:
        await self._session.execute(
            insert(AuditEventRecord).values(
                event_id=event.event_id,
                order_id=event.order_id,
                drone_id=event.drone_id,
                event_type=event.event_type.value,
                previous_state=None if event.previous_state is None else event.previous_state.value,
                new_state=None if event.new_state is None else event.new_state.value,
                occurred_at=event.occurred_at,
                source=event.source.value,
                event_metadata=_thaw(event.metadata),
            )
        )

    async def list_for_order(self, order_id: UUID) -> list[AuditEvent]:
        rows = await self._session.scalars(
            select(AuditEventRecord)
            .where(AuditEventRecord.order_id == order_id)
            .order_by(AuditEventRecord.seq)
        )
        return [_to_domain(row) for row in rows]
