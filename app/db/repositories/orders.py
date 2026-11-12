"""Order repository (Rule 4).

This module and the migration are the only places allowed to write order rows.
The write methods accept a :class:`TransitionResult` and nothing else, so a status
can only be stored if the state machine produced it, and its audit event is
written in the same transaction.
"""

from __future__ import annotations

from typing import Any, cast
from uuid import UUID

from sqlalchemy import CursorResult, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import Row
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import OrderRecord
from app.db.repositories.audit import SqlAlchemyAuditLog
from app.db.spatial import latitude_of, longitude_of, point
from app.domain.models import AuditEventType, Location, Order, OrderStatus, Package
from app.domain.order_state_machine import TransitionResult
from app.services.exceptions import ConcurrentUpdateError


def _order_select() -> Any:
    return select(
        OrderRecord.id,
        OrderRecord.client_id,
        OrderRecord.external_order_id,
        longitude_of(OrderRecord.pickup).label("pickup_lon"),
        latitude_of(OrderRecord.pickup).label("pickup_lat"),
        longitude_of(OrderRecord.dropoff).label("dropoff_lon"),
        latitude_of(OrderRecord.dropoff).label("dropoff_lat"),
        OrderRecord.package_weight_kg,
        OrderRecord.package_description,
        OrderRecord.status,
        OrderRecord.drone_id,
        OrderRecord.created_at,
        OrderRecord.updated_at,
    )


def _to_domain(row: Row[Any]) -> Order:
    return Order.rehydrate(
        id=row.id,
        client_id=row.client_id,
        external_order_id=row.external_order_id,
        pickup=Location(latitude=row.pickup_lat, longitude=row.pickup_lon),
        dropoff=Location(latitude=row.dropoff_lat, longitude=row.dropoff_lon),
        package=Package(weight_kg=row.package_weight_kg, description=row.package_description),
        status=OrderStatus(row.status),
        created_at=row.created_at,
        updated_at=row.updated_at,
        drone_id=row.drone_id,
    )


class SqlAlchemyOrderStore:
    def __init__(self, session: AsyncSession, audit: SqlAlchemyAuditLog) -> None:
        self._session = session
        self._audit = audit

    async def get(self, order_id: UUID, *, for_update: bool = False) -> Order | None:
        statement = _order_select().where(OrderRecord.id == order_id)
        if for_update:
            statement = statement.with_for_update()
        row = (await self._session.execute(statement)).one_or_none()
        return None if row is None else _to_domain(row)

    async def get_by_external_id(self, client_id: str, external_order_id: str) -> Order | None:
        statement = _order_select().where(
            OrderRecord.client_id == client_id,
            OrderRecord.external_order_id == external_order_id,
        )
        row = (await self._session.execute(statement)).one_or_none()
        return None if row is None else _to_domain(row)

    async def insert(self, result: TransitionResult) -> bool:
        order, event = result.order, result.audit_event
        if event.event_type is not AuditEventType.ORDER_CREATED:
            raise ValueError("insert() needs a result produced by OrderStateMachine.create()")

        inserted_id = await self._session.scalar(
            pg_insert(OrderRecord)
            .values(
                id=order.id,
                client_id=order.client_id,
                external_order_id=order.external_order_id,
                pickup=point(order.pickup),
                dropoff=point(order.dropoff),
                package_weight_kg=order.package.weight_kg,
                package_description=order.package.description,
                status=order.status.value,
                drone_id=order.drone_id,
                created_at=order.created_at,
                updated_at=order.updated_at,
            )
            .on_conflict_do_nothing(index_elements=["client_id", "external_order_id"])
            .returning(OrderRecord.id)
        )
        if inserted_id is None:
            return False
        await self._audit.append(event)
        return True

    async def apply_transition(self, result: TransitionResult) -> None:
        order, event = result.order, result.audit_event
        if event.event_type is not AuditEventType.ORDER_STATUS_CHANGED or (
            event.previous_state is None
        ):
            raise ValueError("apply_transition() needs a result produced by transition()")

        # Compare-and-set on the previous status. The state graph has no cycles, so a
        # matching status means nobody else has moved the order since we read it.
        outcome = await self._session.execute(
            update(OrderRecord)
            .where(
                OrderRecord.id == order.id,
                OrderRecord.status == event.previous_state.value,
            )
            .values(
                status=order.status.value,
                drone_id=order.drone_id,
                updated_at=order.updated_at,
            )
            .execution_options(synchronize_session=False)
        )
        if cast("CursorResult[Any]", outcome).rowcount != 1:
            raise ConcurrentUpdateError(
                f"order {order.id} was not in state '{event.previous_state}' when updated"
            )
        await self._audit.append(event)
